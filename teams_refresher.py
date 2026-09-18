#!/usr/bin/env python3
"""Keep Microsoft Teams from flipping to "Away" while you're at the machine.

Teams (web, in a Chromium browser) decides you're idle by reading the
compositor's idle clock. On GNOME/Wayland that clock is Mutter's
org.gnome.Mutter.IdleMonitor. This daemon:

  1. asks Mutter to notify it the moment the idle clock passes a threshold
     (event-driven -- no polling loop),
  2. taps F15 on a virtual keyboard it creates via /dev/uinput.

F15 enters through libinput exactly like a real key, so Mutter's idle clock
resets and Teams keeps seeing you as active -- even with the browser
minimised. No app binds F15 and it doesn't move the pointer, so the nudge is
invisible.

It does nothing while you're actually using the machine, and nothing at all
while the screen is locked (locking is an explicit "I've stepped away").
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import logging
import os
import signal
import struct
import sys
import time

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

try:  # PyGObject >= 3.50 moved the unix signal helper out of GLib
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix

    _unix_signal_add = GLibUnix.signal_add
except (ValueError, ImportError):  # pragma: no cover - older PyGObject
    _unix_signal_add = GLib.unix_signal_add

LOG = logging.getLogger("teams-refresher")

# ---------------------------------------------------------------------------
# uinput / evdev constants (linux/input-event-codes.h, linux/uinput.h)
# ---------------------------------------------------------------------------

EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02
SYN_REPORT = 0
REL_X = 0x00
KEYS = {"F13": 183, "F14": 184, "F15": 185, "F16": 186}

_IOC_WRITE = 1
UINPUT_IOCTL_BASE = ord("U")


def _IO(nr: int) -> int:
    return (UINPUT_IOCTL_BASE << 8) | nr


def _IOW(nr: int, size: int) -> int:
    return (_IOC_WRITE << 30) | (size << 16) | (UINPUT_IOCTL_BASE << 8) | nr


# struct uinput_setup: struct input_id (4x u16) + char name[80] + u32
UINPUT_SETUP_FMT = "HHHH80sI"
UI_DEV_SETUP = _IOW(3, struct.calcsize(UINPUT_SETUP_FMT))
UI_DEV_CREATE = _IO(1)
UI_DEV_DESTROY = _IO(2)
UI_SET_EVBIT = _IOW(100, ctypes.sizeof(ctypes.c_int))
UI_SET_KEYBIT = _IOW(101, ctypes.sizeof(ctypes.c_int))
UI_SET_RELBIT = _IOW(102, ctypes.sizeof(ctypes.c_int))

# struct input_event: struct timeval (2x long) + u16 type + u16 code + s32 value
INPUT_EVENT_FMT = "@llHHi"

DEVICE_NAME = b"Teams Refresher Virtual Input"
BUS_VIRTUAL = 0x06

UINPUT_HELP = """\
Cannot open /dev/uinput for writing.

This is the one privileged bit the daemon needs: permission to create a
virtual input device. Run ./install.sh once to add a udev rule and put you
in the 'input' group, then log out and back in (or use: sg input -c ...).
"""


class VirtualInput:
    """A virtual keyboard (+ optional pointer) backed by /dev/uinput."""

    def __init__(self, key: str = "F15", with_pointer: bool = False) -> None:
        self.keycode = KEYS[key]
        self.key_name = key
        self.with_pointer = with_pointer
        self._fd: int | None = None

    def open(self) -> None:
        try:
            fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise PermissionError(UINPUT_HELP) from exc
            if exc.errno == errno.ENOENT:
                raise RuntimeError(
                    "/dev/uinput is missing. Load the module: sudo modprobe uinput"
                ) from exc
            raise

        try:
            fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
            fcntl.ioctl(fd, UI_SET_KEYBIT, self.keycode)
            if self.with_pointer:
                fcntl.ioctl(fd, UI_SET_EVBIT, EV_REL)
                fcntl.ioctl(fd, UI_SET_RELBIT, REL_X)

            setup = struct.pack(
                UINPUT_SETUP_FMT, BUS_VIRTUAL, 0x1209, 0x7EA3, 1, DEVICE_NAME, 0
            )
            fcntl.ioctl(fd, UI_DEV_SETUP, setup)
            fcntl.ioctl(fd, UI_DEV_CREATE)
        except OSError:
            os.close(fd)
            raise

        self._fd = fd
        # Give udev/libinput a moment to enumerate the device, otherwise the
        # very first event can be emitted before the compositor is listening.
        time.sleep(0.2)
        LOG.debug("virtual input device created (key=%s)", self.key_name)

    def _emit(self, etype: int, code: int, value: int) -> None:
        assert self._fd is not None
        os.write(self._fd, struct.pack(INPUT_EVENT_FMT, 0, 0, etype, code, value))

    def _sync(self) -> None:
        self._emit(EV_SYN, SYN_REPORT, 0)

    def nudge(self) -> None:
        """Emit one invisible activity event."""
        self._emit(EV_KEY, self.keycode, 1)
        self._sync()
        self._emit(EV_KEY, self.keycode, 0)
        self._sync()
        if self.with_pointer:
            # Round trip: the pointer ends exactly where it started.
            self._emit(EV_REL, REL_X, 1)
            self._sync()
            self._emit(EV_REL, REL_X, -1)
            self._sync()

    def close(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.ioctl(self._fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None
        LOG.debug("virtual input device destroyed")

    def __enter__(self) -> "VirtualInput":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# GNOME session plumbing
# ---------------------------------------------------------------------------

MUTTER_NAME = "org.gnome.Mutter.IdleMonitor"
MUTTER_PATH = "/org/gnome/Mutter/IdleMonitor/Core"
SAVER_NAME = "org.gnome.ScreenSaver"
SAVER_PATH = "/org/gnome/ScreenSaver"


class Refresher:
    def __init__(self, threshold_s: int, device: VirtualInput, allow_locked: bool,
                 dry_run: bool) -> None:
        self.threshold_ms = threshold_s * 1000
        self.device = device
        self.allow_locked = allow_locked
        self.dry_run = dry_run

        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.loop = GLib.MainLoop()
        self.locked = False
        self.nudges = 0
        self.watch_id: int | None = None
        self._subscriptions: list[int] = []
        # Hard floor between nudges. Mutter fires an idle watch immediately if
        # the clock is already past the threshold, so without this a nudge that
        # fails to reset the clock would spin the event loop.
        self.cooldown_s = max(5, threshold_s // 10)
        self._last_nudge = float("-inf")
        self._ineffective = 0

    # -- D-Bus helpers ------------------------------------------------------

    def _call(self, name: str, path: str, method: str, args: GLib.Variant | None,
              reply_type: str | None) -> tuple | None:
        try:
            reply = self.bus.call_sync(
                name, path, name, method, args,
                GLib.VariantType(reply_type) if reply_type else None,
                Gio.DBusCallFlags.NONE, 3000, None,
            )
        except GLib.Error as exc:
            LOG.warning("D-Bus %s.%s failed: %s", name, method, exc.message)
            return None
        return reply.unpack() if reply is not None else None

    def idle_ms(self) -> int | None:
        result = self._call(MUTTER_NAME, MUTTER_PATH, "GetIdletime", None, "(t)")
        return result[0] if result else None

    def query_locked(self) -> bool:
        result = self._call(SAVER_NAME, SAVER_PATH, "GetActive", None, "(b)")
        return bool(result[0]) if result else False

    # -- core behaviour -----------------------------------------------------

    def maybe_nudge(self, reason: str) -> bool:
        """Emit one activity event if it is both needed and permitted."""
        since = time.monotonic() - self._last_nudge
        if since < self.cooldown_s:
            LOG.debug("skipping nudge (%s): cooling down, %.0fs since last",
                      reason, since)
            return False

        if self.locked and not self.allow_locked:
            LOG.debug("skipping nudge (%s): screen is locked", reason)
            return False

        idle = self.idle_ms()
        if idle is not None and idle < self.threshold_ms:
            # Real activity beat us to it; nothing to do.
            LOG.debug("skipping nudge (%s): idle %.0fs below threshold",
                      reason, idle / 1000)
            return False

        self._last_nudge = time.monotonic()
        self.nudges += 1
        if self.dry_run:
            LOG.info("[dry-run] would nudge #%d (%s, idle %ss)",
                     self.nudges, reason, "?" if idle is None else round(idle / 1000))
            return True

        try:
            self.device.nudge()
        except OSError as exc:
            LOG.error("failed to emit nudge: %s -- recreating device", exc)
            self._recreate_device()
            return False

        LOG.info("nudge #%d sent (%s) -- %s tapped, idle clock reset",
                 self.nudges, reason, self.device.key_name)
        # Mutter sees the event asynchronously, so confirm shortly after.
        GLib.timeout_add(300, self._verify_nudge)
        return True

    def _verify_nudge(self) -> bool:
        """Warn if our synthetic input is not actually resetting the clock."""
        idle = self.idle_ms()
        if idle is None:
            return GLib.SOURCE_REMOVE
        if idle >= self.threshold_ms:
            self._ineffective += 1
            if self._ineffective in (1, 5, 20):
                LOG.warning(
                    "nudge did not reset the idle clock (still %.0fs) -- %dx now. "
                    "The virtual device may not be reaching the compositor; try "
                    "--pointer, or check that /dev/uinput is writable.",
                    idle / 1000, self._ineffective)
        else:
            if self._ineffective:
                LOG.info("idle clock responding again")
            self._ineffective = 0
            if self.watch_id is None:
                self._arm_idle_watch()
        return GLib.SOURCE_REMOVE

    def _recreate_device(self) -> None:
        try:
            self.device.close()
            self.device.open()
        except Exception as exc:  # noqa: BLE001 - last-resort recovery
            LOG.error("could not recreate virtual device: %s", exc)

    # -- watches ------------------------------------------------------------

    def _disarm_idle_watch(self) -> None:
        if self.watch_id is None:
            return
        self._call(MUTTER_NAME, MUTTER_PATH, "RemoveWatch",
                   GLib.Variant("(u)", (self.watch_id,)), None)
        self.watch_id = None

    def _arm_idle_watch(self) -> None:
        """Ask Mutter to fire a signal once the idle clock passes threshold."""
        result = self._call(
            MUTTER_NAME, MUTTER_PATH, "AddIdleWatch",
            GLib.Variant("(t)", (self.threshold_ms,)), "(u)",
        )
        if result:
            self.watch_id = result[0]
            LOG.debug("idle watch armed (id=%s, %ss)",
                      self.watch_id, self.threshold_ms // 1000)
        else:
            self.watch_id = None
            LOG.warning("could not arm idle watch; falling back to safety poll")

    def _on_watch_fired(self, _conn, _sender, _path, _iface, _signal,
                        params: GLib.Variant) -> None:
        (fired_id,) = params.unpack()
        if fired_id != self.watch_id:
            return
        self.maybe_nudge("idle watch")
        # A watch fires when the clock *crosses* the threshold, so it will not
        # fire again by itself. Drop it and re-arm -- but only once the clock
        # has actually gone back below the threshold, otherwise Mutter fires
        # the new watch instantly and we spin. If the clock is still high,
        # _verify_nudge or the backstop poll re-arms us later.
        self._disarm_idle_watch()
        idle = self.idle_ms()
        if idle is not None and idle < self.threshold_ms:
            self._arm_idle_watch()
        else:
            LOG.debug("idle still high; deferring re-arm")

    def _on_lock_changed(self, _conn, _sender, _path, _iface, _signal,
                         params: GLib.Variant) -> None:
        (active,) = params.unpack()
        self.locked = bool(active)
        LOG.info("screen %s -- %s", "locked" if self.locked else "unlocked",
                 "pausing" if self.locked and not self.allow_locked else "active")

    def _on_mutter_appeared(self, *_args) -> None:
        """gnome-shell restarted: our watch died with it, so re-arm."""
        LOG.info("Mutter available -- (re)arming idle watch")
        self._arm_idle_watch()

    def _on_mutter_vanished(self, *_args) -> None:
        LOG.warning("Mutter went away (gnome-shell restart?) -- watch invalidated")
        self.watch_id = None

    def _safety_poll(self) -> bool:
        """Belt-and-braces: catch a dropped watch or a missed signal."""
        if self.watch_id is None:
            self._arm_idle_watch()
        self.maybe_nudge("safety poll")
        return GLib.SOURCE_CONTINUE

    # -- lifecycle ----------------------------------------------------------

    def run(self) -> int:
        self.locked = self.query_locked()

        self._subscriptions.append(self.bus.signal_subscribe(
            None, MUTTER_NAME, "WatchFired", MUTTER_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_watch_fired))
        self._subscriptions.append(self.bus.signal_subscribe(
            None, SAVER_NAME, "ActiveChanged", SAVER_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_lock_changed))

        Gio.bus_watch_name_on_connection(
            self.bus, MUTTER_NAME, Gio.BusNameWatcherFlags.NONE,
            self._on_mutter_appeared, self._on_mutter_vanished)

        # Backstop for a dropped watch or missed signal. Kept well under the
        # threshold so a failed watch still nudges before Teams' 5min cutoff;
        # it is only a GetIdletime D-Bus call, so it costs effectively nothing.
        self.poll_s = max(15, self.threshold_ms // 4000)
        GLib.timeout_add_seconds(self.poll_s, self._safety_poll)

        for sig in (signal.SIGINT, signal.SIGTERM):
            _unix_signal_add(GLib.PRIORITY_DEFAULT, sig, self._quit)

        LOG.info("watching: nudge after %ss idle (backstop poll %ss), key=%s, "
                 "locked-screen=%s%s",
                 self.threshold_ms // 1000, self.poll_s, self.device.key_name,
                 "active" if self.allow_locked else "paused",
                 ", DRY RUN" if self.dry_run else "")
        self.loop.run()
        LOG.info("stopped after %d nudge(s)", self.nudges)
        return 0

    def _quit(self) -> bool:
        LOG.info("shutting down")
        self._disarm_idle_watch()
        self.loop.quit()
        return GLib.SOURCE_REMOVE


# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="teams-refresher",
        description="Hold Microsoft Teams 'Available' by resetting the GNOME idle clock.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-t", "--threshold", type=int, default=180, metavar="SECONDS",
                   help="idle time before a nudge (default: 180, comfortably "
                        "inside the 5 minutes Teams waits before going Away)")
    p.add_argument("-k", "--key", choices=sorted(KEYS), default="F15",
                   help="which no-op key to tap (default: F15)")
    p.add_argument("--pointer", action="store_true",
                   help="also jiggle the pointer 1px and back, for clients that "
                        "only watch mouse movement (not needed for Brave/Teams)")
    p.add_argument("--allow-locked", action="store_true",
                   help="keep nudging even when the screen is locked")
    p.add_argument("--once", action="store_true",
                   help="send a single nudge and exit (useful for testing)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="log what would happen without emitting any input")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--status", action="store_true",
                   help="print current idle time and lock state, then exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.status:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        probe = Refresher.__new__(Refresher)
        probe.bus = bus
        idle = Refresher.idle_ms(probe)
        print(f"idle:   {'unknown' if idle is None else f'{idle / 1000:.1f}s'}")
        print(f"locked: {Refresher.query_locked(probe)}")
        print(f"uinput: {'writable' if os.access('/dev/uinput', os.W_OK) else 'NOT writable (run ./install.sh)'}")
        return 0

    device = VirtualInput(key=args.key, with_pointer=args.pointer)
    try:
        if args.once:
            if args.dry_run:
                LOG.info("[dry-run] would tap %s once", args.key)
                return 0
            with device:
                device.nudge()
            LOG.info("single %s nudge sent", args.key)
            return 0

        if args.dry_run:
            return Refresher(args.threshold, device, args.allow_locked, True).run()

        with device:
            return Refresher(args.threshold, device, args.allow_locked, False).run()
    except PermissionError as exc:
        print(exc, file=sys.stderr)
        return 13
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
