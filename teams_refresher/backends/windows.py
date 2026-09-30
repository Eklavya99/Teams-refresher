"""Windows backend.

Everything here is one ctypes call into user32:

  * SendInput() injects the key. Injected input updates the same
    session-wide input timestamp that real hardware does, which is what
    GetLastInputInfo reports and what Chromium's idle detection reads -- so
    Teams sees the reset even with the browser minimised.
  * GetLastInputInfo() is that timestamp, and our own nudge resets it. That
    circularity is the point: it is the exact clock Teams is watching.
  * OpenInputDesktop() fails for a normal process while the secure (lock)
    desktop has the input, which is a cheap, dependency-free lock check.

No admin rights and nothing to install: unlike Linux's /dev/uinput, a normal
user process may synthesise input into its own session.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
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

# Virtual-key codes (winuser.h). F13-F16 are the quiet end of the keyboard.
VK_CODES = {"F13": 0x7C, "F14": 0x7D, "F15": 0x7E, "F16": 0x7F}

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_KEYUP = 0x0002
MOUSEEVENTF_MOVE = 0x0001
DESKTOP_SWITCHDESKTOP = 0x0100

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def _user32():
    try:
        return ctypes.WinDLL("user32", use_last_error=True)
    except (AttributeError, OSError) as exc:  # pragma: no cover - not Windows
        raise BackendUnavailable("user32 unavailable: %s" % exc) from exc


class SendInputInjector(Injector):
    """Synthesise the keystroke with SendInput()."""

    name = "SendInput virtual key"

    def __init__(self, key: str = "F15", with_pointer: bool = False) -> None:
        super().__init__(key, with_pointer)
        self.vk = VK_CODES[key]
        self._user32 = _user32()
        self._user32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
        self._user32.SendInput.restype = wintypes.UINT

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def _send(self, events: Sequence[_INPUT]) -> None:
        count = len(events)
        array = (_INPUT * count)(*events)
        sent = self._user32.SendInput(count, ctypes.byref(array), ctypes.sizeof(_INPUT))
        if sent != count:
            raise OSError(
                ctypes.get_last_error(),
                "SendInput delivered %d of %d events" % (sent, count),
            )

    def _key_event(self, down: bool) -> _INPUT:
        event = _INPUT(type=INPUT_KEYBOARD)
        event.ki = _KEYBDINPUT(
            wVk=self.vk, wScan=0, dwFlags=0 if down else KEYEVENTF_KEYUP,
            time=0, dwExtraInfo=0,
        )
        return event

    def _move_event(self, dx: int) -> _INPUT:
        event = _INPUT(type=INPUT_MOUSE)
        event.mi = _MOUSEINPUT(
            dx=dx, dy=0, mouseData=0, dwFlags=MOUSEEVENTF_MOVE, time=0, dwExtraInfo=0
        )
        return event

    def nudge(self) -> None:
        events = [self._key_event(True), self._key_event(False)]
        if self.with_pointer:
            # Relative move out and back: the pointer ends where it started.
            events += [self._move_event(1), self._move_event(-1)]
        self._send(events)


class LastInputIdleMonitor(IdleMonitor):
    """Seconds since the last input event anywhere in this session."""

    name = "GetLastInputInfo"

    def __init__(self) -> None:
        self._user32 = _user32()
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.GetTickCount.restype = wintypes.DWORD
        self._info = _LASTINPUTINFO()
        self._info.cbSize = ctypes.sizeof(_LASTINPUTINFO)

    def idle_seconds(self) -> Optional[float]:
        if not self._user32.GetLastInputInfo(ctypes.byref(self._info)):
            return None
        # dwTime comes from the 32-bit GetTickCount, so subtract in 32 bits:
        # the difference stays correct across the ~49-day wrap.
        elapsed = (self._kernel32.GetTickCount() - self._info.dwTime) & 0xFFFFFFFF
        return elapsed / 1000.0


class DesktopLockMonitor(LockMonitor):
    """Locked means the secure desktop owns the input, and we can't open it."""

    name = "input desktop"

    def __init__(self) -> None:
        self._user32 = _user32()
        self._user32.OpenInputDesktop.argtypes = [
            wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
        ]
        self._user32.OpenInputDesktop.restype = wintypes.HANDLE
        self._user32.CloseDesktop.argtypes = [wintypes.HANDLE]

    def is_locked(self) -> Optional[bool]:
        handle = self._user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not handle:
            return True
        self._user32.CloseDesktop(handle)
        return False


# ---------------------------------------------------------------------------
# Autostart: the per-user Run key
# ---------------------------------------------------------------------------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "TeamsRefresher"


class RunKeyService(Service):
    """Start at logon via HKCU\\...\\Run.

    A registry value rather than a Windows service or a scheduled task: those
    need administrator rights, and a real service runs in session 0 where its
    synthetic input would never reach your desktop. This has to run as you,
    in your session, which is exactly what the Run key gives.
    """

    name = "HKCU Run key"

    def __init__(self) -> None:
        try:
            import winreg  # noqa: F401
        except ImportError as exc:  # pragma: no cover - not Windows
            raise BackendUnavailable("winreg unavailable") from exc

    def _open(self, access):
        import winreg

        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, access)

    def install(self, argv: Sequence[str]) -> str:
        import winreg

        command = subprocess.list2cmdline(list(argv))
        with self._open(winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
        return (
            "registered at logon as %s\n  command: %s\n"
            "  It starts at your next sign-in; to start it now, just run "
            "teams-refresher in a terminal." % (RUN_VALUE, command)
        )

    def uninstall(self) -> str:
        import winreg

        try:
            with self._open(winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, RUN_VALUE)
        except FileNotFoundError:
            return "nothing was installed"
        return "removed the %s logon entry" % RUN_VALUE

    def status(self) -> str:
        import winreg

        try:
            with self._open(winreg.KEY_READ) as key:
                command, _ = winreg.QueryValueEx(key, RUN_VALUE)
        except FileNotFoundError:
            return "not installed"
        return "runs at logon: %s" % command


# ---------------------------------------------------------------------------


class WindowsPlatform(Platform):
    name = "Windows"

    def make_injector(self, key: str, with_pointer: bool) -> Injector:
        return SendInputInjector(key, with_pointer)

    def make_idle_monitor(self) -> IdleMonitor:
        return LastInputIdleMonitor()

    def make_lock_monitor(self) -> LockMonitor:
        return DesktopLockMonitor()

    def make_service(self) -> Service:
        try:
            return RunKeyService()
        except BackendUnavailable as exc:
            return UnsupportedService(str(exc))

    def describe(self) -> List[Tuple[str, str]]:
        version = getattr(sys, "getwindowsversion", None)
        release = ".".join(str(n) for n in version().platform_version) if version else "?"
        return [
            ("session", os.environ.get("SESSIONNAME", "unknown")),
            ("windows", release),
        ]

    @staticmethod
    def windowless_python() -> str:
        """pythonw.exe next to this interpreter, so the logon start is silent."""
        executable = sys.executable or ""
        candidate = executable.replace("python.exe", "pythonw.exe")
        if candidate != executable and os.path.exists(candidate):
            return candidate
        return executable
