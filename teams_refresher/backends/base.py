"""The contract every platform backend fills in.

The daemon only ever needs four things from the operating system, so that is
all a backend has to provide:

    Injector     emit one invisible input event
    IdleMonitor  how long since the user last did anything
    LockMonitor  is the session locked (an explicit "I've stepped away")
    Service      start me again at login

Only the first is mandatory. A platform that cannot answer "how idle are
you?" still works -- the core loop falls back to a blind timer -- and one
that cannot tell locked from unlocked simply never pauses. Degrading is
always better than refusing to run, because the nudge itself is harmless.
"""

from __future__ import annotations

import abc
import logging
from typing import List, Optional, Sequence, Tuple

_LOG = logging.getLogger("teams-refresher")

KEY_CHOICES = ("F13", "F14", "F15", "F16")


class BackendUnavailable(RuntimeError):
    """This implementation cannot work here; the caller should try the next."""


class Injector(abc.ABC):
    """Emits the synthetic activity event."""

    #: Human-readable, shown by --status.
    name = "injector"

    def __init__(self, key: str = "F15", with_pointer: bool = False) -> None:
        if key not in KEY_CHOICES:
            raise ValueError("key must be one of %s" % (KEY_CHOICES,))
        self.key_name = key
        self.with_pointer = with_pointer

    @abc.abstractmethod
    def open(self) -> None:
        """Acquire whatever handle is needed. Raises on permission problems."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release it. Must be safe to call when never opened."""

    @abc.abstractmethod
    def nudge(self) -> None:
        """Emit one activity event."""

    def check(self) -> str:
        """Open and close once, reporting 'ready' or why not. For --status."""
        try:
            self.open()
        except Exception as exc:  # noqa: BLE001 - this is the reporting path
            message = str(exc).strip()
            return message.splitlines()[0] if message else type(exc).__name__
        self.close()
        return "ready"

    def __enter__(self) -> "Injector":
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class IdleMonitor(abc.ABC):
    """Reads the same idle clock the browser reads."""

    name = "idle monitor"

    @abc.abstractmethod
    def idle_seconds(self) -> Optional[float]:
        """Seconds since the last input, or None if it cannot be determined."""


class NoIdleMonitor(IdleMonitor):
    """Last resort: no idle clock on this system, so nudge on a fixed cadence."""

    name = "none (blind timer)"

    def idle_seconds(self) -> Optional[float]:
        return None


class LockMonitor(abc.ABC):
    """Reports whether the session is locked."""

    name = "lock monitor"

    @abc.abstractmethod
    def is_locked(self) -> Optional[bool]:
        """True, False, or None when the state is unknown."""


class NoLockMonitor(LockMonitor):
    name = "none (never pauses)"

    def is_locked(self) -> Optional[bool]:
        return None


class Service(abc.ABC):
    """Whatever this OS calls 'run this at login': systemd, launchd, Run key."""

    name = "service"

    @abc.abstractmethod
    def install(self, argv: Sequence[str]) -> str:
        """Register argv to run at login. Returns a line to print."""

    @abc.abstractmethod
    def uninstall(self) -> str:
        """Remove the registration. Returns a line to print."""

    @abc.abstractmethod
    def status(self) -> str:
        """One line describing the current registration."""


class UnsupportedService(Service):
    name = "unsupported"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def install(self, argv: Sequence[str]) -> str:
        raise BackendUnavailable(self.reason)

    def uninstall(self) -> str:
        raise BackendUnavailable(self.reason)

    def status(self) -> str:
        return self.reason


class Platform(abc.ABC):
    """Chooses the best available implementation of each of the four."""

    name = "unknown"

    @abc.abstractmethod
    def make_injector(self, key: str, with_pointer: bool) -> Injector:
        ...

    def make_idle_monitor(self) -> IdleMonitor:
        return NoIdleMonitor()

    def make_lock_monitor(self) -> LockMonitor:
        return NoLockMonitor()

    def make_service(self) -> Service:
        return UnsupportedService("no login-service integration for this platform")

    def describe(self) -> List[Tuple[str, str]]:
        """Extra (label, value) rows for --status; environment detail, mostly."""
        return []


def first_available(candidates, fallback):
    """Construct the first candidate that works, else the fallback.

    Each candidate is a zero-argument factory. A backend that cannot work in
    this session says so by raising BackendUnavailable from its constructor,
    which is why probing happens at construction time rather than on first use.
    """
    for factory in candidates:
        try:
            return factory()
        except BackendUnavailable as exc:
            _LOG.debug("backend %s unavailable: %s", _name_of(factory), exc)
        except Exception as exc:  # noqa: BLE001 - a broken probe must not be fatal
            _LOG.debug("backend %s failed to probe: %r", _name_of(factory), exc)
    return fallback()


def _name_of(factory) -> str:
    return getattr(factory, "__name__", type(factory).__name__)
