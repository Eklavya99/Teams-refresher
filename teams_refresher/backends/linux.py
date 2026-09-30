"""Linux backend.

Input goes in through /dev/uinput: a virtual device that enters the stack via
libinput exactly like real hardware, which on Wayland is the only thing that
resets the compositor's idle clock. Where that isn't permitted but the session
is X11, XTEST is a serviceable second choice.

The idle clock itself is read from Mutter on GNOME, from the freedesktop
screensaver interface on KDE, and from the X11 XScreenSaver extension
otherwise. All three are reset by the virtual device, which is the only
property the core loop actually depends on.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import logging
import os
import shlex
import shutil
import struct
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .base import (
    BackendUnavailable,
    IdleMonitor,
    Injector,
    LockMonitor,
    NoIdleMonitor,
    NoLockMonitor,
    Platform,
    Service,
    UnsupportedService,
    first_available,
)

LOG = logging.getLogger("teams-refresher")

# ---------------------------------------------------------------------------
# uinput / evdev constants (linux/input-event-codes.h, linux/uinput.h)
# ---------------------------------------------------------------------------

EV_SYN, EV_KEY, EV_REL = 0x00, 0x01, 0x02
SYN_REPORT = 0
REL_X = 0x00
KEYCODES = {"F13": 183, "F14": 184, "F15": 185, "F16": 186}

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


class UinputInjector(Injector):
    """A virtual keyboard (+ optional pointer) backed by /dev/uinput."""

    name = "uinput virtual keyboard"

    def __init__(self, key: str = "F15", with_pointer: bool = False) -> None:
        super().__init__(key, with_pointer)
        self.keycode = KEYCODES[key]
        self._fd: Optional[int] = None

    def open(self) -> None:
        try:
            fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EPERM):
                raise PermissionError(UINPUT_HELP) from exc
            if exc.errno == errno.ENOENT:
                raise BackendUnavailable(
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


# ---------------------------------------------------------------------------
# X11: XTEST injection and XScreenSaver idle, for sessions without uinput
# ---------------------------------------------------------------------------

XK_F13 = 0xFFCA  # keysyms are contiguous from here: F13, F14, F15, F16
KEYSYMS = {name: XK_F13 + i for i, name in enumerate(("F13", "F14", "F15", "F16"))}


def _open_x_display():
    """Open the X display, or explain why we can't. Returns (libX11, display)."""
    if not os.environ.get("DISPLAY"):
        raise BackendUnavailable("no DISPLAY -- not an X11 session")
    try:
        libx11 = ctypes.CDLL("libX11.so.6")
    except OSError as exc:
        raise BackendUnavailable("libX11 not present: %s" % exc) from exc
    libx11.XOpenDisplay.restype = ctypes.c_void_p
    libx11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    display = libx11.XOpenDisplay(None)
    if not display:
        raise BackendUnavailable("cannot open DISPLAY %s" % os.environ["DISPLAY"])
    return libx11, ctypes.c_void_p(display)


class XTestInjector(Injector):
    """Fake key events through the X server's XTEST extension.

    Only useful on a real X11 session: under Wayland this reaches XWayland,
    which has its own idle clock that the compositor pays no attention to.
    """

    name = "X11 XTEST"

    def __init__(self, key: str = "F15", with_pointer: bool = False) -> None:
        super().__init__(key, with_pointer)
        if (os.environ.get("XDG_SESSION_TYPE") or "").lower() == "wayland":
            raise BackendUnavailable("XTEST cannot reach a Wayland compositor")
        self._x11, self._dpy = _open_x_display()
        try:
            self._xtst = ctypes.CDLL("libXtst.so.6")
        except OSError as exc:
            raise BackendUnavailable("libXtst not present: %s" % exc) from exc
        self._x11.XKeysymToKeycode.restype = ctypes.c_ubyte
        self._x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        self.keycode = self._x11.XKeysymToKeycode(self._dpy, KEYSYMS[key])
        if not self.keycode:
            raise BackendUnavailable("%s is not mapped in this keyboard layout" % key)

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def nudge(self) -> None:
        self._xtst.XTestFakeKeyEvent(self._dpy, self.keycode, True, 0)
        self._xtst.XTestFakeKeyEvent(self._dpy, self.keycode, False, 0)
        if self.with_pointer:
            self._xtst.XTestFakeRelativeMotionEvent(self._dpy, 1, 0, 0)
            self._xtst.XTestFakeRelativeMotionEvent(self._dpy, -1, 0, 0)
        self._x11.XFlush(self._dpy)


class _XScreenSaverInfo(ctypes.Structure):
    _fields_ = [
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("til_or_since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),
        ("event_mask", ctypes.c_ulong),
    ]


class XScreenSaverIdleMonitor(IdleMonitor):
    """Idle time straight from the X server. Works on any X11 desktop."""

    name = "X11 XScreenSaver"

    def __init__(self) -> None:
        self._x11, self._dpy = _open_x_display()
        try:
            self._xss = ctypes.CDLL("libXss.so.1")
        except OSError as exc:
            raise BackendUnavailable("libXss not present: %s" % exc) from exc
        self._xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(_XScreenSaverInfo)
        self._x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self._x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self._root = self._x11.XDefaultRootWindow(self._dpy)
        self._info = self._xss.XScreenSaverAllocInfo()
        if self.idle_seconds() is None:
            raise BackendUnavailable("XScreenSaver extension not answering")

    def idle_seconds(self) -> Optional[float]:
        ok = self._xss.XScreenSaverQueryInfo(self._dpy, self._root, self._info)
        if not ok:
            return None
        return self._info.contents.idle / 1000.0


# ---------------------------------------------------------------------------
# D-Bus, without dragging in a main loop
# ---------------------------------------------------------------------------


class SessionBus:
    """A one-shot session-bus caller.

    Prefers PyGObject when it is installed (near-universal on GNOME) and
    shells out to gdbus otherwise. Both are asked only for single scalar
    replies, so the reply is normalised to a string and parsed by the caller.
    """

    def __init__(self) -> None:
        self.flavour = ""
        self._bus = None
        self._gdbus = None
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib  # noqa: F401

            self._gio = Gio
            self._glib = GLib
            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            self.flavour = "PyGObject"
        except Exception:  # noqa: BLE001 - any failure means "use gdbus"
            self._gdbus = shutil.which("gdbus")
            if self._gdbus is None:
                raise BackendUnavailable(
                    "no session-bus client (install python3-gi, or gdbus)"
                )
            self.flavour = "gdbus"

    def call(self, dest: str, path: str, iface: str, method: str) -> Optional[str]:
        """Call a no-argument method; return its single return value as text."""
        if self._bus is not None:
            try:
                reply = self._bus.call_sync(
                    dest, path, iface, method, None, None,
                    self._gio.DBusCallFlags.NONE, 3000, None,
                )
            except Exception as exc:  # noqa: BLE001 - GLib.Error and friends
                LOG.debug("D-Bus %s.%s failed: %s", iface, method, exc)
                return None
            values = reply.unpack() if reply is not None else ()
            return str(values[0]) if values else None

        cmd = [
            self._gdbus, "call", "--session", "--dest", dest,
            "--object-path", path, "--method", "%s.%s" % (iface, method),
        ]
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=5, check=True
            ).stdout
        except (subprocess.SubprocessError, OSError) as exc:
            LOG.debug("gdbus %s.%s failed: %s", iface, method, exc)
            return None
        # gdbus prints replies as a tuple literal: "(uint64 26565,)", "(true,)"
        text = out.strip().lstrip("(").rstrip(")").rstrip(",").strip()
        parts = text.split()
        return parts[-1] if parts else None

    @staticmethod
    def as_int(value: Optional[str]) -> Optional[int]:
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def as_bool(value: Optional[str]) -> Optional[bool]:
        if value is None:
            return None
        return value.strip().lower() in ("true", "1")


class _DBusIdleMonitor(IdleMonitor):
    """Shared shape for the two D-Bus idle clocks; subclasses set the address."""

    dest = path = iface = method = ""
    divisor = 1000.0  # reply units per second

    def __init__(self, bus: Optional[SessionBus] = None) -> None:
        self.bus = bus or SessionBus()
        if self.idle_seconds() is None:
            raise BackendUnavailable("%s not answering" % self.dest)

    def idle_seconds(self) -> Optional[float]:
        raw = self.bus.as_int(
            self.bus.call(self.dest, self.path, self.iface, self.method)
        )
        return None if raw is None else raw / self.divisor


class MutterIdleMonitor(_DBusIdleMonitor):
    """GNOME's idle clock -- the one Chromium reports to Teams on Wayland."""

    name = "GNOME Mutter IdleMonitor"
    dest = iface = "org.gnome.Mutter.IdleMonitor"
    path = "/org/gnome/Mutter/IdleMonitor/Core"
    method = "GetIdletime"


class FreedesktopIdleMonitor(_DBusIdleMonitor):
    """KDE and others implement this on the screensaver service. Seconds, not ms."""

    name = "freedesktop ScreenSaver idle"
    dest = iface = "org.freedesktop.ScreenSaver"
    path = "/org/freedesktop/ScreenSaver"
    method = "GetSessionIdleTime"
    divisor = 1.0


class _DBusLockMonitor(LockMonitor):
    dest = path = iface = ""

    def __init__(self, bus: Optional[SessionBus] = None) -> None:
        self.bus = bus or SessionBus()
        if self.is_locked() is None:
            raise BackendUnavailable("%s not answering" % self.dest)

    def is_locked(self) -> Optional[bool]:
        return self.bus.as_bool(
            self.bus.call(self.dest, self.path, self.iface, "GetActive")
        )


class GnomeLockMonitor(_DBusLockMonitor):
    name = "GNOME ScreenSaver"
    dest = iface = "org.gnome.ScreenSaver"
    path = "/org/gnome/ScreenSaver"


class FreedesktopLockMonitor(_DBusLockMonitor):
    name = "freedesktop ScreenSaver"
    dest = iface = "org.freedesktop.ScreenSaver"
    path = "/org/freedesktop/ScreenSaver"


# ---------------------------------------------------------------------------
# systemd user unit
# ---------------------------------------------------------------------------

UNIT_NAME = "teams-refresher.service"

UNIT_TEMPLATE = """\
[Unit]
Description=Teams presence refresher (keeps the idle clock from tripping Teams "Away")
PartOf=graphical-session.target
After=graphical-session.target

[Service]
Type=simple
ExecStart={exec_start}
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=5
# The daemon needs the session bus and /dev/uinput and nothing else, but the
# sandboxing options are left off on purpose: several of them are unreliable
# in user units across systemd versions, and a service that refuses to start
# is worse than one that is merely unconfined.
NoNewPrivileges=true

[Install]
WantedBy=graphical-session.target
"""


class SystemdService(Service):
    name = "systemd user unit"

    def __init__(self) -> None:
        if not shutil.which("systemctl"):
            raise BackendUnavailable("systemctl not found -- no systemd here")
        self.unit_path = (
            Path.home() / ".config" / "systemd" / "user" / UNIT_NAME
        )

    def _systemctl(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", "--user", *args], capture_output=True, text=True
        )

    def install(self, argv: Sequence[str]) -> str:
        self.unit_path.parent.mkdir(parents=True, exist_ok=True)
        exec_start = " ".join(shlex.quote(part) for part in argv)
        self.unit_path.write_text(UNIT_TEMPLATE.format(exec_start=exec_start))
        self._systemctl("daemon-reload")
        result = self._systemctl("enable", "--now", UNIT_NAME)
        if result.returncode != 0:
            return (
                "wrote %s, but could not enable it:\n%s\n"
                "Enable it from inside your graphical session with:\n"
                "  systemctl --user enable --now %s"
                % (self.unit_path, result.stderr.strip(), UNIT_NAME)
            )
        return "installed and started %s\n  logs: journalctl --user -u %s -f" % (
            self.unit_path,
            UNIT_NAME,
        )

    def uninstall(self) -> str:
        self._systemctl("disable", "--now", UNIT_NAME)
        removed = self.unit_path.exists()
        self.unit_path.unlink(missing_ok=True)
        self._systemctl("daemon-reload")
        return "removed %s" % self.unit_path if removed else "nothing was installed"

    def status(self) -> str:
        if not self.unit_path.exists():
            return "not installed"
        enabled = self._systemctl("is-enabled", UNIT_NAME).stdout.strip() or "unknown"
        active = self._systemctl("is-active", UNIT_NAME).stdout.strip() or "unknown"
        return "%s, %s (%s)" % (enabled, active, self.unit_path)


# ---------------------------------------------------------------------------


class LinuxPlatform(Platform):
    name = "Linux"

    def __init__(self) -> None:
        self._bus: Optional[SessionBus] = None
        self._bus_probed = False

    def _session_bus(self) -> SessionBus:
        """One bus connection, shared by the idle and lock monitors."""
        if not self._bus_probed:
            self._bus_probed = True
            try:
                self._bus = SessionBus()
            except BackendUnavailable:
                self._bus = None
        if self._bus is None:
            raise BackendUnavailable("no session bus")
        return self._bus

    def make_injector(self, key: str, with_pointer: bool) -> Injector:
        # uinput first: it is the only one that reaches a Wayland compositor.
        # XTEST is the consolation prize for an X11 box without uinput rights.
        try:
            return UinputInjector(key, with_pointer)
        except (PermissionError, BackendUnavailable) as exc:
            try:
                injector = XTestInjector(key, with_pointer)
            except Exception:  # noqa: BLE001 - report the original problem
                raise exc
            LOG.warning("falling back to XTEST: %s", str(exc).splitlines()[0])
            return injector

    def make_idle_monitor(self) -> IdleMonitor:
        return first_available(
            [
                lambda: MutterIdleMonitor(self._session_bus()),
                lambda: FreedesktopIdleMonitor(self._session_bus()),
                XScreenSaverIdleMonitor,
            ],
            NoIdleMonitor,
        )

    def make_lock_monitor(self) -> LockMonitor:
        return first_available(
            [
                lambda: GnomeLockMonitor(self._session_bus()),
                lambda: FreedesktopLockMonitor(self._session_bus()),
            ],
            NoLockMonitor,
        )

    def make_service(self) -> Service:
        try:
            return SystemdService()
        except BackendUnavailable as exc:
            return UnsupportedService(str(exc))

    def describe(self) -> List[Tuple[str, str]]:
        session = os.environ.get("XDG_SESSION_TYPE", "unknown")
        desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
        rows = [("session", "%s on %s" % (desktop, session))]
        rows.append(
            ("uinput", "writable" if os.access("/dev/uinput", os.W_OK)
             else "NOT writable (run ./install.sh)")
        )
        return rows
