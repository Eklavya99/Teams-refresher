package platform

// Linux (GNOME on Wayland): Teams in a Chromium browser asks the browser
// whether you're idle, and Chromium reads the compositor's idle clock --
// Mutter's org.gnome.Mutter.IdleMonitor. We ask Mutter to signal us when that
// clock passes the threshold, then tap a key on a virtual keyboard created via
// /dev/uinput. It enters through libinput like real hardware, so Mutter's
// clock resets even with the browser minimised.

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"
	"unsafe"

	"github.com/godbus/dbus/v5"
	"golang.org/x/sys/unix"

	"github.com/eklavya99/teams-refresher/internal/logx"
)

const IneffectiveHint = "The virtual device may not be reaching the compositor; try " +
	"--pointer, or check that /dev/uinput is writable."

// ---------------------------------------------------------------------------
// uinput / evdev constants (linux/input-event-codes.h, linux/uinput.h)
// ---------------------------------------------------------------------------

const (
	evSyn     = 0x00
	evKey     = 0x01
	evRel     = 0x02
	synReport = 0
	relX      = 0x00

	busVirtual = 0x06
	deviceName = "Teams Refresher Virtual Input"

	uinputIoctlBase = 'U'
)

var linuxKeycodes = map[string]uint16{"F13": 183, "F14": 184, "F15": 185, "F16": 186}

// struct uinput_setup: struct input_id (4x u16) + char name[80] + u32
type uinputSetup struct {
	Bustype, Vendor, Product, Version uint16
	Name                              [80]byte
	FFEffectsMax                      uint32
}

// struct input_event: struct timeval (2x long) + u16 type + u16 code + s32 value
type inputEvent struct {
	Sec, Usec  int // C long on Linux
	Type, Code uint16
	Value      int32
}

func ioc(nr uintptr) uintptr { return uinputIoctlBase<<8 | nr }
func iow(nr, size uintptr) uintptr {
	return 1<<30 | size<<16 | uinputIoctlBase<<8 | nr
}

var (
	uiDevCreate  = ioc(1)
	uiDevDestroy = ioc(2)
	uiDevSetup   = iow(3, unsafe.Sizeof(uinputSetup{}))
	uiSetEvbit   = iow(100, 4)
	uiSetKeybit  = iow(101, 4)
	uiSetRelbit  = iow(102, 4)
)

const uinputHelp = `cannot open /dev/uinput for writing.

This is the one privileged bit the daemon needs: permission to create a
virtual input device. Run scripts/linux/install.sh once to add a udev rule and put you
in the 'input' group, then log out and back in (or use: sg input -c ...).`

// virtualInput is a virtual keyboard (+ optional pointer) backed by /dev/uinput.
type virtualInput struct {
	keycode uint16
	pointer bool
	fd      int
}

func (v *virtualInput) open() error {
	fd, err := unix.Open("/dev/uinput", unix.O_WRONLY|unix.O_NONBLOCK|unix.O_CLOEXEC, 0)
	switch {
	case errors.Is(err, unix.EACCES), errors.Is(err, unix.EPERM):
		return fmt.Errorf("%w: %s", ErrPermission, uinputHelp)
	case errors.Is(err, unix.ENOENT):
		return errors.New("/dev/uinput is missing. Load the module: sudo modprobe uinput")
	case err != nil:
		return fmt.Errorf("open /dev/uinput: %w", err)
	}

	setup := uinputSetup{Bustype: busVirtual, Vendor: 0x1209, Product: 0x7EA3, Version: 1}
	copy(setup.Name[:], deviceName)

	err = func() error {
		if err := unix.IoctlSetInt(fd, uint(uiSetEvbit), evKey); err != nil {
			return err
		}
		if err := unix.IoctlSetInt(fd, uint(uiSetKeybit), int(v.keycode)); err != nil {
			return err
		}
		if v.pointer {
			if err := unix.IoctlSetInt(fd, uint(uiSetEvbit), evRel); err != nil {
				return err
			}
			if err := unix.IoctlSetInt(fd, uint(uiSetRelbit), relX); err != nil {
				return err
			}
		}
		if _, _, e := unix.Syscall(unix.SYS_IOCTL, uintptr(fd), uiDevSetup, uintptr(unsafe.Pointer(&setup))); e != 0 {
			return e
		}
		if _, _, e := unix.Syscall(unix.SYS_IOCTL, uintptr(fd), uiDevCreate, 0); e != 0 {
			return e
		}
		return nil
	}()
	if err != nil {
		unix.Close(fd)
		return fmt.Errorf("set up uinput device: %w", err)
	}

	v.fd = fd
	// Give udev/libinput a moment to enumerate the device, otherwise the very
	// first event can be emitted before the compositor is listening.
	time.Sleep(200 * time.Millisecond)
	logx.Debug("virtual input device created")
	return nil
}

func (v *virtualInput) emit(typ, code uint16, value int32) error {
	ev := inputEvent{Type: typ, Code: code, Value: value}
	buf := unsafe.Slice((*byte)(unsafe.Pointer(&ev)), unsafe.Sizeof(ev))
	_, err := unix.Write(v.fd, buf)
	return err
}

func (v *virtualInput) nudge() error {
	seq := [][3]int32{
		{evKey, int32(v.keycode), 1}, {evSyn, synReport, 0},
		{evKey, int32(v.keycode), 0}, {evSyn, synReport, 0},
	}
	if v.pointer {
		// Round trip: the pointer ends exactly where it started.
		seq = append(seq,
			[3]int32{evRel, relX, 1}, [3]int32{evSyn, synReport, 0},
			[3]int32{evRel, relX, -1}, [3]int32{evSyn, synReport, 0})
	}
	for _, e := range seq {
		if err := v.emit(uint16(e[0]), uint16(e[1]), e[2]); err != nil {
			return err
		}
	}
	return nil
}

func (v *virtualInput) close() {
	if v.fd <= 0 {
		return
	}
	unix.Syscall(unix.SYS_IOCTL, uintptr(v.fd), uiDevDestroy, 0)
	unix.Close(v.fd)
	v.fd = 0
	logx.Debug("virtual input device destroyed")
}

// ---------------------------------------------------------------------------
// GNOME session plumbing
// ---------------------------------------------------------------------------

const (
	mutterName = "org.gnome.Mutter.IdleMonitor"
	mutterPath = "/org/gnome/Mutter/IdleMonitor/Core"
	saverName  = "org.gnome.ScreenSaver"
	saverPath  = "/org/gnome/ScreenSaver"
)

type linuxPlatform struct {
	opts   Options
	conn   *dbus.Conn
	dev    *virtualInput
	events chan Event
	sigs   chan *dbus.Signal

	mu      sync.Mutex
	watchID uint32
	armed   bool
}

func New(opts Options) (Platform, error) {
	conn, err := dbus.ConnectSessionBus()
	if err != nil {
		return nil, fmt.Errorf("connect to the session D-Bus (is this a GNOME desktop session?): %w", err)
	}
	p := &linuxPlatform{opts: opts, conn: conn}

	if opts.Input {
		p.dev = &virtualInput{keycode: linuxKeycodes[opts.Key], pointer: opts.Pointer}
		if err := p.dev.open(); err != nil {
			conn.Close()
			return nil, err
		}
	}

	if opts.Watch {
		p.events = make(chan Event, 8)
		p.sigs = make(chan *dbus.Signal, 16)
		matches := [][]dbus.MatchOption{
			{dbus.WithMatchInterface(mutterName), dbus.WithMatchMember("WatchFired"), dbus.WithMatchObjectPath(mutterPath)},
			{dbus.WithMatchInterface(saverName), dbus.WithMatchMember("ActiveChanged"), dbus.WithMatchObjectPath(saverPath)},
			{dbus.WithMatchInterface("org.freedesktop.DBus"), dbus.WithMatchMember("NameOwnerChanged"), dbus.WithMatchArg(0, mutterName)},
		}
		for _, m := range matches {
			if err := conn.AddMatchSignal(m...); err != nil {
				logx.Warn("could not subscribe to D-Bus signal: %v", err)
			}
		}
		conn.Signal(p.sigs)
		go p.dispatch()
	}
	return p, nil
}

func (p *linuxPlatform) call(dest, path, method string, out any, args ...any) error {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	c := p.conn.Object(dest, dbus.ObjectPath(path)).CallWithContext(ctx, dest+"."+method, 0, args...)
	if c.Err != nil {
		return fmt.Errorf("D-Bus %s.%s: %w", dest, method, c.Err)
	}
	if out != nil {
		return c.Store(out)
	}
	return nil
}

func (p *linuxPlatform) IdleTime() (time.Duration, error) {
	var ms uint64
	if err := p.call(mutterName, mutterPath, "GetIdletime", &ms); err != nil {
		logx.Warn("%v", err)
		return 0, err
	}
	return time.Duration(ms) * time.Millisecond, nil
}

func (p *linuxPlatform) Locked() (bool, error) {
	var active bool
	if err := p.call(saverName, saverPath, "GetActive", &active); err != nil {
		logx.Warn("%v", err)
		return false, err
	}
	return active, nil
}

func (p *linuxPlatform) Nudge() error {
	if p.dev == nil {
		return errors.New("no input device (dry-run?)")
	}
	err := p.dev.nudge()
	if err == nil {
		return nil
	}
	logx.Error("failed to emit nudge: %v -- recreating device", err)
	p.dev.close()
	if rerr := p.dev.open(); rerr != nil {
		logx.Error("could not recreate virtual device: %v", rerr)
	}
	return err
}

func (p *linuxPlatform) Events() <-chan Event { return p.events }

// Rearm asks Mutter for a new idle watch if we have none -- but only once the
// clock is back below the threshold. A watch fires when the clock *crosses*
// the threshold, and Mutter fires a new one instantly if the clock is already
// past it, so arming early would spin.
func (p *linuxPlatform) Rearm() {
	if !p.opts.Watch {
		return
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.armed {
		return
	}
	var ms uint64
	if err := p.call(mutterName, mutterPath, "GetIdletime", &ms); err != nil {
		return
	}
	if time.Duration(ms)*time.Millisecond >= p.opts.Threshold {
		logx.Debug("idle still high; deferring re-arm")
		return
	}
	var id uint32
	if err := p.call(mutterName, mutterPath, "AddIdleWatch", &id, uint64(p.opts.Threshold.Milliseconds())); err != nil {
		logx.Warn("could not arm idle watch (%v); relying on the backstop poll", err)
		return
	}
	p.watchID, p.armed = id, true
	logx.Debug("idle watch armed (id=%d, %s)", id, p.opts.Threshold)
}

func (p *linuxPlatform) disarmLocked() {
	if !p.armed {
		return
	}
	_ = p.call(mutterName, mutterPath, "RemoveWatch", nil, p.watchID)
	p.armed = false
}

func (p *linuxPlatform) dispatch() {
	for s := range p.sigs {
		switch s.Name {
		case mutterName + ".WatchFired":
			if len(s.Body) < 1 {
				continue
			}
			id, _ := s.Body[0].(uint32)
			p.mu.Lock()
			mine := p.armed && id == p.watchID
			if mine {
				// Fired watches don't fire again; drop it so Rearm makes a new one.
				p.disarmLocked()
			}
			p.mu.Unlock()
			if mine {
				p.events <- Event{Kind: IdleCrossed}
			}
		case saverName + ".ActiveChanged":
			if len(s.Body) < 1 {
				continue
			}
			active, _ := s.Body[0].(bool)
			p.events <- Event{Kind: LockChanged, Locked: active}
		case "org.freedesktop.DBus.NameOwnerChanged":
			if len(s.Body) < 3 {
				continue
			}
			newOwner, _ := s.Body[2].(string)
			p.mu.Lock()
			p.armed = false // our watch died with the old gnome-shell
			p.mu.Unlock()
			if newOwner == "" {
				logx.Warn("Mutter went away (gnome-shell restart?) -- watch invalidated")
			} else {
				logx.Info("Mutter available -- (re)arming idle watch")
				p.Rearm()
			}
		}
	}
}

func (p *linuxPlatform) StatusLines() []string {
	state := "writable"
	if unix.Access("/dev/uinput", unix.W_OK) != nil {
		state = "NOT writable (run scripts/linux/install.sh)"
	}
	return []string{"uinput: " + state}
}

func (p *linuxPlatform) Close() error {
	p.mu.Lock()
	p.disarmLocked()
	p.mu.Unlock()
	if p.dev != nil {
		p.dev.close()
	}
	return p.conn.Close()
}

// ---------------------------------------------------------------------------
// OS hooks used by cmd/teams-refresher
// ---------------------------------------------------------------------------

func PrepareOutput(daemon bool) {}

func SingleInstance() (release func(), ok bool) { return func() {}, true }

func InstallService([]string) error {
	return errors.New("on Linux, use scripts/linux/install.sh and the systemd user service")
}

func UninstallService() error {
	return errors.New("on Linux, use scripts/linux/uninstall.sh")
}
