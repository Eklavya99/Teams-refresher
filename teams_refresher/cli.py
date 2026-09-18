"""Command line: argument parsing, --status, and the login-service commands."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
from pathlib import Path
from typing import List, Optional, Sequence

from . import __version__
from .backends import UnsupportedPlatform, current_platform
from .backends.base import KEY_CHOICES, BackendUnavailable, Platform
from .core import Refresher

LOG = logging.getLogger("teams-refresher")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="teams-refresher",
        description="Hold Microsoft Teams 'Available' by keeping the system "
                    "idle clock from reaching its Away threshold. "
                    "Runs on Linux, macOS and Windows.",
    )
    parser.add_argument("-t", "--threshold", type=float, default=180, metavar="SECONDS",
                        help="idle time before a nudge (default: 180, comfortably "
                             "inside the 5 minutes Teams waits before going Away)")
    parser.add_argument("-k", "--key", choices=list(KEY_CHOICES), default="F15",
                        help="which no-op key to tap (default: F15)")
    parser.add_argument("--pointer", action="store_true",
                        help="also jiggle the pointer 1px and back, for clients "
                             "that only watch mouse movement")
    parser.add_argument("--allow-locked", action="store_true",
                        help="keep nudging even when the screen is locked")
    parser.add_argument("--once", action="store_true",
                        help="send a single nudge and exit (useful for testing)")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="log what would happen without emitting any input")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--log-file", metavar="PATH",
                        help="also append logs here (rotated at 1 MB); useful "
                             "when running detached at login")
    parser.add_argument("--status", action="store_true",
                        help="print the chosen backends, idle time and lock "
                             "state, then exit")
    parser.add_argument("--version", action="version",
                        version="teams-refresher %s" % __version__)

    service = parser.add_argument_group(
        "login service",
        "Register the daemon to start when you log in: a systemd user unit on "
        "Linux, a launchd agent on macOS, a Run key entry on Windows. The "
        "other options given alongside --install-service are baked into it.",
    )
    service.add_argument("--install-service", action="store_true",
                         help="install and start the login service")
    service.add_argument("--uninstall-service", action="store_true",
                         help="stop and remove it")
    service.add_argument("--service-status", action="store_true",
                         help="report whether it is installed")
    return parser


def setup_logging(verbose: bool, log_file: Optional[str]) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        path = Path(log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            logging.handlers.RotatingFileHandler(
                str(path), maxBytes=1_000_000, backupCount=2, encoding="utf-8"
            )
        )
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


# ---------------------------------------------------------------------------
# --status
# ---------------------------------------------------------------------------


def print_status(platform: Platform, args: argparse.Namespace) -> int:
    rows = [("version", __version__), ("platform", platform.name)]
    rows.extend(platform.describe())

    try:
        injector = platform.make_injector(args.key, args.pointer)
        rows.append(("inject", "%s (%s) -- %s"
                     % (injector.name, args.key, injector.check())))
    except Exception as exc:  # noqa: BLE001 - --status must never itself fail
        rows.append(("inject", "unavailable: %s" % str(exc).splitlines()[0]))

    idle_monitor = platform.make_idle_monitor()
    idle = idle_monitor.idle_seconds()
    rows.append(("idle", "%s -- %s" % (
        idle_monitor.name, "unknown" if idle is None else "%.1fs" % idle)))

    lock_monitor = platform.make_lock_monitor()
    locked = lock_monitor.is_locked()
    rows.append(("lock", "%s -- %s" % (
        lock_monitor.name,
        "unknown" if locked is None else ("locked" if locked else "unlocked"))))

    try:
        service = platform.make_service()
        rows.append(("service", "%s -- %s" % (service.name, service.status())))
    except Exception as exc:  # noqa: BLE001
        rows.append(("service", "unavailable: %s" % exc))

    width = max(len(label) for label, _ in rows) + 1  # room for the colon
    for label, value in rows:
        print("%-*s  %s" % (width, label + ":", value))
    return 0


# ---------------------------------------------------------------------------
# login service
# ---------------------------------------------------------------------------


def service_argv(args: argparse.Namespace) -> List[str]:
    """The command line the login service should run.

    Built from an absolute interpreter path and an absolute script path, so it
    does not depend on PATH, on a working directory, or on the shell that ran
    --install-service still existing.
    """
    python = sys.executable
    if sys.platform.startswith("win"):
        from .backends.windows import WindowsPlatform

        python = WindowsPlatform.windowless_python()

    argv = [python, str(Path(__file__).resolve().parent / "__main__.py"),
            "--threshold", "%g" % args.threshold, "--key", args.key]
    if args.pointer:
        argv.append("--pointer")
    if args.allow_locked:
        argv.append("--allow-locked")
    if args.verbose:
        argv.append("--verbose")
    if args.log_file:
        argv.extend(["--log-file", str(Path(args.log_file).expanduser())])
    elif sys.platform.startswith("win"):
        # Nothing captures stdout from a Run key entry, so give it a log by
        # default -- otherwise a failure there is completely invisible.
        argv.extend(["--log-file", str(
            Path(os.environ.get("LOCALAPPDATA", Path.home()))
            / "teams-refresher" / "teams-refresher.log")])
    return argv


def run_service_command(platform: Platform, args: argparse.Namespace) -> int:
    service = platform.make_service()
    try:
        if args.install_service:
            print(service.install(service_argv(args)))
        elif args.uninstall_service:
            print(service.uninstall())
        else:
            print("%s: %s" % (service.name, service.status()))
    except BackendUnavailable as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose, args.log_file)

    try:
        platform = current_platform()
    except UnsupportedPlatform as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    if args.status:
        return print_status(platform, args)
    if args.install_service or args.uninstall_service or args.service_status:
        return run_service_command(platform, args)

    try:
        injector = platform.make_injector(args.key, args.pointer)

        if args.once:
            if args.dry_run:
                LOG.info("[dry-run] would tap %s once", args.key)
                return 0
            with injector:
                injector.nudge()
            LOG.info("single %s nudge sent", args.key)
            return 0

        refresher = Refresher(
            threshold=args.threshold,
            injector=injector,
            idle_monitor=platform.make_idle_monitor(),
            lock_monitor=platform.make_lock_monitor(),
            allow_locked=args.allow_locked,
            dry_run=args.dry_run,
        )
        install_signal_handlers(refresher)

        if args.dry_run:
            return refresher.run()
        with injector:
            return refresher.run()
    except PermissionError as exc:
        print(exc, file=sys.stderr)
        return 13
    except (BackendUnavailable, RuntimeError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


def install_signal_handlers(refresher: Refresher) -> None:
    """Stop cleanly on the signals each OS actually sends.

    SIGTERM is what systemd and launchd send; Windows has no SIGTERM for
    console apps, so only the ones that exist are registered.
    """
    def handler(_signum: int, _frame: object) -> None:
        LOG.info("shutting down")
        refresher.stop()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, handler)
        except (ValueError, OSError):  # not the main thread, or not supported
            pass


if __name__ == "__main__":
    sys.exit(main())
