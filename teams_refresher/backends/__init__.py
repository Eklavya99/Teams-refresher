"""Backend selection: one look at sys.platform, then never again."""

from __future__ import annotations

import sys

from .base import (  # noqa: F401 - re-exported for callers
    KEY_CHOICES,
    BackendUnavailable,
    IdleMonitor,
    Injector,
    LockMonitor,
    Platform,
    Service,
)


class UnsupportedPlatform(RuntimeError):
    pass


def current_platform() -> Platform:
    """The Platform for whatever we are running on.

    Imports are deferred into the branches on purpose: each backend module
    reaches straight for OS-specific libraries (wintypes, CoreGraphics,
    fcntl), and importing the other two would fail.
    """
    if sys.platform.startswith("linux"):
        from .linux import LinuxPlatform

        return LinuxPlatform()
    if sys.platform == "darwin":
        from .macos import MacPlatform

        return MacPlatform()
    if sys.platform.startswith("win") or sys.platform == "cygwin":
        from .windows import WindowsPlatform

        return WindowsPlatform()
    raise UnsupportedPlatform(
        "no backend for platform %r. Linux, macOS and Windows are supported."
        % sys.platform
    )
