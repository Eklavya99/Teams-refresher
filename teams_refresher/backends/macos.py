"""macOS backend.

Quartz, reached through ctypes so there is nothing to pip install:

  * CGEventPost() injects the key. It needs Accessibility permission for
    whichever app is running Python (Terminal, iTerm, or the python binary
    itself) -- the one setup step on this platform, and the daemon says so
    plainly instead of failing silently.
  * CGEventSourceSecondsSinceLastEventType() with the *combined* session
    state is the idle clock Chromium reads. Combined counts posted events as
    well as hardware, which is precisely why our nudge resets it. (The
    HID-only clock would not budge, and we would spin.)
  * CGSessionCopyCurrentDictionary() carries CGSSessionScreenIsLocked.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import plistlib
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .base import (
    BackendUnavailable,
    IdleMonitor,
    Injector,
    LockMonitor,
    Platform,
    Service,
    UnsupportedService,
)

# Virtual key codes (HIToolbox Events.h). The F-keys are not contiguous here.
KEYCODES = {"F13": 0x69, "F14": 0x6B, "F15": 0x71, "F16": 0x6A}

kCGHIDEventTap = 0
kCGEventMouseMoved = 5
kCGEventSourceStateCombinedSessionState = 0
kCGAnyInputEventType = 0xFFFFFFFF
kCFStringEncodingUTF8 = 0x08000100

ACCESSIBILITY_HELP = """\
macOS is refusing synthetic input.

Grant Accessibility permission to whatever runs this daemon -- your terminal
app for a foreground run, or the python binary itself for the launchd agent:

  System Settings -> Privacy & Security -> Accessibility -> +

Then run it again. (Toggle it off and on after upgrading Python: macOS keys
the grant to the exact binary.)
"""


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _load_frameworks():
    """Return (CoreGraphics, CoreFoundation), or say why we can't."""
    quartz = None
    for path in (
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",
    ):
        try:
            quartz = ctypes.cdll.LoadLibrary(path)
            break
        except OSError:
            continue
    if quartz is None:
        raise BackendUnavailable("cannot load CoreGraphics")

    cf_path = ctypes.util.find_library("CoreFoundation")
    try:
        core_foundation = ctypes.cdll.LoadLibrary(
            cf_path or "/System/Library/Frameworks/"
            "CoreFoundation.framework/CoreFoundation"
        )
    except OSError as exc:
        raise BackendUnavailable("cannot load CoreFoundation: %s" % exc) from exc

    quartz.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
    quartz.CGEventCreateKeyboardEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool
    ]
    quartz.CGEventCreateMouseEvent.restype = ctypes.c_void_p
    quartz.CGEventCreateMouseEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, CGPoint, ctypes.c_uint32
    ]
    quartz.CGEventCreate.restype = ctypes.c_void_p
    quartz.CGEventCreate.argtypes = [ctypes.c_void_p]
    quartz.CGEventGetLocation.restype = CGPoint
    quartz.CGEventGetLocation.argtypes = [ctypes.c_void_p]
    quartz.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
    quartz.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
    quartz.CGEventSourceSecondsSinceLastEventType.argtypes = [
        ctypes.c_uint32, ctypes.c_uint32
    ]
    core_foundation.CFRelease.argtypes = [ctypes.c_void_p]
    return quartz, core_foundation


class QuartzInjector(Injector):
    """Post the keystroke onto the HID event tap."""

    name = "Quartz CGEventPost"

    def __init__(self, key: str = "F15", with_pointer: bool = False) -> None:
        super().__init__(key, with_pointer)
        self.keycode = KEYCODES[key]
        self._quartz, self._cf = _load_frameworks()

    def open(self) -> None:
        # Posting works only for a process trusted for Accessibility. Ask up
        # front so the failure is a sentence, not a nudge that quietly does
        # nothing. (AXIsProcessTrusted lives in ApplicationServices; if it is
        # not reachable we simply skip the check and let the post speak.)
        trusted = getattr(self._quartz, "AXIsProcessTrusted", None)
        if trusted is not None:
            trusted.restype = ctypes.c_bool
            if not trusted():
                raise PermissionError(ACCESSIBILITY_HELP)

    def close(self) -> None:
        return None

    def _post(self, event: Optional[int]) -> None:
        if not event:
            raise OSError("CoreGraphics refused to create the event")
        self._quartz.CGEventPost(kCGHIDEventTap, ctypes.c_void_p(event))
        self._cf.CFRelease(ctypes.c_void_p(event))

    def _pointer_location(self) -> Optional[CGPoint]:
        probe = self._quartz.CGEventCreate(None)
        if not probe:
            return None
        where = self._quartz.CGEventGetLocation(ctypes.c_void_p(probe))
        self._cf.CFRelease(ctypes.c_void_p(probe))
        return where

    def nudge(self) -> None:
        self._post(self._quartz.CGEventCreateKeyboardEvent(None, self.keycode, True))
        self._post(self._quartz.CGEventCreateKeyboardEvent(None, self.keycode, False))
        if self.with_pointer:
            where = self._pointer_location()
            if where is None:
                return
            for point in (CGPoint(where.x + 1, where.y), where):
                self._post(
                    self._quartz.CGEventCreateMouseEvent(
                        None, kCGEventMouseMoved, point, 0
                    )
                )


class QuartzIdleMonitor(IdleMonitor):
    name = "Quartz event source"

    def __init__(self) -> None:
        self._quartz, _ = _load_frameworks()

    def idle_seconds(self) -> Optional[float]:
        seconds = self._quartz.CGEventSourceSecondsSinceLastEventType(
            kCGEventSourceStateCombinedSessionState, kCGAnyInputEventType
        )
        return None if seconds < 0 else float(seconds)


class QuartzLockMonitor(LockMonitor):
    name = "CGSession dictionary"

    def __init__(self) -> None:
        self._quartz, self._cf = _load_frameworks()
        copy_session = getattr(self._quartz, "CGSessionCopyCurrentDictionary", None)
        if copy_session is None:
            raise BackendUnavailable("CGSessionCopyCurrentDictionary unavailable")
        copy_session.restype = ctypes.c_void_p
        self._copy_session = copy_session
        self._cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self._cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32
        ]
        self._cf.CFDictionaryGetValue.restype = ctypes.c_void_p
        self._cf.CFDictionaryGetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._cf.CFBooleanGetValue.restype = ctypes.c_bool
        self._cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]

    def is_locked(self) -> Optional[bool]:
        session = self._copy_session()
        if not session:
            return None
        key = self._cf.CFStringCreateWithCString(
            None, b"CGSSessionScreenIsLocked", kCFStringEncodingUTF8
        )
        try:
            value = self._cf.CFDictionaryGetValue(
                ctypes.c_void_p(session), ctypes.c_void_p(key)
            )
            # The key is absent entirely while unlocked, so absence is False.
            return bool(value) and bool(self._cf.CFBooleanGetValue(ctypes.c_void_p(value)))
        finally:
            if key:
                self._cf.CFRelease(ctypes.c_void_p(key))
            self._cf.CFRelease(ctypes.c_void_p(session))


# ---------------------------------------------------------------------------
# launchd
# ---------------------------------------------------------------------------

LABEL = "io.github.teams-refresher"


class LaunchdService(Service):
    """A per-user LaunchAgent: it runs inside your GUI session, as you."""

    name = "launchd agent"

    def __init__(self) -> None:
        if not os.path.exists("/bin/launchctl"):
            raise BackendUnavailable("launchctl not found")
        self.plist_path = Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % LABEL)
        self.log_path = Path.home() / "Library" / "Logs" / "teams-refresher.log"
        self.domain = "gui/%d" % os.getuid()

    def _launchctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["/bin/launchctl", *args], capture_output=True, text=True
        )

    def install(self, argv: Sequence[str]) -> str:
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        plist = {
            "Label": LABEL,
            "ProgramArguments": list(argv),
            "RunAtLoad": True,
            "KeepAlive": True,
            "ProcessType": "Background",
            "StandardOutPath": str(self.log_path),
            "StandardErrorPath": str(self.log_path),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        }
        with open(self.plist_path, "wb") as handle:
            plistlib.dump(plist, handle)

        # bootout first so a reinstall picks up the new arguments.
        self._launchctl("bootout", "%s/%s" % (self.domain, LABEL))
        result = self._launchctl("bootstrap", self.domain, str(self.plist_path))
        if result.returncode != 0:
            # Older systems only know load/unload.
            result = self._launchctl("load", "-w", str(self.plist_path))
        if result.returncode != 0:
            return "wrote %s, but launchctl refused it:\n%s" % (
                self.plist_path, result.stderr.strip() or result.stdout.strip()
            )
        return "installed and started %s\n  logs: tail -f %s" % (
            self.plist_path, self.log_path
        )

    def uninstall(self) -> str:
        self._launchctl("bootout", "%s/%s" % (self.domain, LABEL))
        self._launchctl("unload", "-w", str(self.plist_path))
        existed = self.plist_path.exists()
        self.plist_path.unlink(missing_ok=True)
        return "removed %s" % self.plist_path if existed else "nothing was installed"

    def status(self) -> str:
        if not self.plist_path.exists():
            return "not installed"
        listed = self._launchctl("list", LABEL)
        state = "loaded" if listed.returncode == 0 else "not loaded"
        return "%s (%s)" % (state, self.plist_path)


# ---------------------------------------------------------------------------


class MacPlatform(Platform):
    name = "macOS"

    def make_injector(self, key: str, with_pointer: bool) -> Injector:
        return QuartzInjector(key, with_pointer)

    def make_idle_monitor(self) -> IdleMonitor:
        return QuartzIdleMonitor()

    def make_lock_monitor(self) -> LockMonitor:
        try:
            return QuartzLockMonitor()
        except BackendUnavailable:
            from .base import NoLockMonitor

            return NoLockMonitor()

    def make_service(self) -> Service:
        try:
            return LaunchdService()
        except BackendUnavailable as exc:
            return UnsupportedService(str(exc))

    def describe(self) -> List[Tuple[str, str]]:
        import platform

        return [("session", "macOS %s (%s)" % (platform.mac_ver()[0], platform.machine()))]
