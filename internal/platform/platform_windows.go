package platform

// Windows: Teams (the desktop app, or Teams in Edge/Chrome) decides you're
// idle from the system-wide last-input time -- the same clock
// GetLastInputInfo reads. A key tapped through SendInput resets that clock
// exactly like real hardware. Windows has no "tell me when idle crosses N"
// API, so we poll GetLastInputInfo every few seconds; it is a single cheap
// call into user32.

import (
	"errors"
	"fmt"
	"sync"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

const IneffectiveHint = "SendInput may be blocked (for example while an elevated " +
	"window has focus, or on the secure desktop); try --pointer."

var (
	user32   = windows.NewLazySystemDLL("user32.dll")
	kernel32 = windows.NewLazySystemDLL("kernel32.dll")

	procGetLastInputInfo         = user32.NewProc("GetLastInputInfo")
	procSendInput                = user32.NewProc("SendInput")
	procOpenInputDesktop         = user32.NewProc("OpenInputDesktop")
	procCloseDesktop             = user32.NewProc("CloseDesktop")
	procGetUserObjectInformation = user32.NewProc("GetUserObjectInformationW")
	procGetTickCount             = kernel32.NewProc("GetTickCount")
	procGetConsoleWindow         = kernel32.NewProc("GetConsoleWindow")
	procGetConsoleProcessList    = kernel32.NewProc("GetConsoleProcessList")
	procFreeConsole              = kernel32.NewProc("FreeConsole")
)

const (
	inputMouse    = 0
	inputKeyboard = 1

	keyeventfKeyUp  = 0x0002
	mouseeventfMove = 0x0001

	desktopSwitchDesktop = 0x0100
	uoiName              = 2

	pollEvery = 5 * time.Second
)

var windowsVKs = map[string]uint16{"F13": 0x7C, "F14": 0x7D, "F15": 0x7E, "F16": 0x7F}

type lastInputInfo struct {
	cbSize uint32
	dwTime uint32
}

type mouseInput struct {
	dx, dy      int32
	mouseData   uint32
	dwFlags     uint32
	time        uint32
	dwExtraInfo uintptr
}

type keybdInput struct {
	wVk, wScan  uint16
	dwFlags     uint32
	time        uint32
	dwExtraInfo uintptr
}

// input mirrors the Win32 INPUT struct: a DWORD type followed by a union whose
// largest member is MOUSEINPUT. Go's alignment rules give the same layout
// (40 bytes on 64-bit, 28 on 32-bit).
type input struct {
	typ uint32
	mi  mouseInput
}

func keyInput(vk uint16, flags uint32) input {
	in := input{typ: inputKeyboard}
	*(*keybdInput)(unsafe.Pointer(&in.mi)) = keybdInput{wVk: vk, dwFlags: flags}
	return in
}

func moveInput(dx int32) input {
	return input{typ: inputMouse, mi: mouseInput{dx: dx, dwFlags: mouseeventfMove}}
}

type windowsPlatform struct {
	opts   Options
	vk     uint16
	events chan Event
	stop   chan struct{}
	once   sync.Once
}

func New(opts Options) (Platform, error) {
	p := &windowsPlatform{opts: opts, vk: windowsVKs[opts.Key], stop: make(chan struct{})}
	if opts.Watch {
		p.events = make(chan Event, 8)
		go p.watch()
	}
	return p, nil
}

func (p *windowsPlatform) IdleTime() (time.Duration, error) {
	lii := lastInputInfo{cbSize: uint32(unsafe.Sizeof(lastInputInfo{}))}
	if r, _, err := procGetLastInputInfo.Call(uintptr(unsafe.Pointer(&lii))); r == 0 {
		return 0, fmt.Errorf("GetLastInputInfo: %w", err)
	}
	now, _, _ := procGetTickCount.Call()
	// Both are 32-bit millisecond tick counts; unsigned subtraction stays
	// correct across the 49.7-day wrap.
	return time.Duration(uint32(now)-lii.dwTime) * time.Millisecond, nil
}

// Locked reports whether the input desktop is something other than the
// user's "Default" desktop -- i.e. the lock screen (or a UAC prompt).
func (p *windowsPlatform) Locked() (bool, error) {
	h, _, _ := procOpenInputDesktop.Call(0, 0, desktopSwitchDesktop)
	if h == 0 {
		// Access to the input desktop is denied while the session is locked.
		return true, nil
	}
	defer procCloseDesktop.Call(h)
	var buf [64]uint16
	var needed uint32
	r, _, err := procGetUserObjectInformation.Call(h, uoiName,
		uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)*2), uintptr(unsafe.Pointer(&needed)))
	if r == 0 {
		return false, fmt.Errorf("GetUserObjectInformation: %w", err)
	}
	return windows.UTF16ToString(buf[:]) != "Default", nil
}

func (p *windowsPlatform) Nudge() error {
	seq := []input{keyInput(p.vk, 0), keyInput(p.vk, keyeventfKeyUp)}
	if p.opts.Pointer {
		// Round trip: the pointer ends exactly where it started.
		seq = append(seq, moveInput(1), moveInput(-1))
	}
	n, _, err := procSendInput.Call(uintptr(len(seq)), uintptr(unsafe.Pointer(&seq[0])), unsafe.Sizeof(seq[0]))
	if int(n) != len(seq) {
		return fmt.Errorf("SendInput inserted %d of %d events: %w", n, len(seq), err)
	}
	return nil
}

func (p *windowsPlatform) Events() <-chan Event { return p.events }

func (p *windowsPlatform) Rearm() {}

// watch turns the polled idle/lock state into edge-triggered events.
func (p *windowsPlatform) watch() {
	t := time.NewTicker(pollEvery)
	defer t.Stop()
	wasHigh := false
	wasLocked, _ := p.Locked()
	for {
		select {
		case <-p.stop:
			return
		case <-t.C:
		}
		if locked, err := p.Locked(); err == nil && locked != wasLocked {
			wasLocked = locked
			p.send(Event{Kind: LockChanged, Locked: locked})
		}
		idle, err := p.IdleTime()
		if err != nil {
			continue
		}
		high := idle >= p.opts.Threshold
		if high && !wasHigh {
			p.send(Event{Kind: IdleCrossed})
		}
		wasHigh = high
	}
}

func (p *windowsPlatform) send(ev Event) {
	select {
	case p.events <- ev:
	case <-p.stop:
	}
}

func (p *windowsPlatform) StatusLines() []string {
	return []string{"input:  SendInput (no setup needed)"}
}

func (p *windowsPlatform) Close() error {
	p.once.Do(func() { close(p.stop) })
	return nil
}

// singleInstance stops a second daemon (say, a manual run while the login
// copy is going) from doubling up the nudges.
func SingleInstance() (release func(), ok bool) {
	name, _ := windows.UTF16PtrFromString(`Local\teams-refresher`)
	h, err := windows.CreateMutex(nil, false, name)
	if errors.Is(err, windows.ERROR_ALREADY_EXISTS) {
		if h != 0 {
			windows.CloseHandle(h)
		}
		return func() {}, false
	}
	if h == 0 {
		return func() {}, true // can't tell; don't block startup over it
	}
	return func() { windows.CloseHandle(h) }, true
}
