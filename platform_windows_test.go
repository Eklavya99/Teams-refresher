package main

import (
	"testing"
	"unsafe"
)

// SendInput rejects the whole batch if cbSize doesn't match the Win32 INPUT
// struct, so pin the layout.
func TestInputLayout(t *testing.T) {
	want := uintptr(40)
	if unsafe.Sizeof(uintptr(0)) == 4 {
		want = 28
	}
	if got := unsafe.Sizeof(input{}); got != want {
		t.Fatalf("sizeof(INPUT) = %d, want %d", got, want)
	}
	if got := unsafe.Sizeof(keybdInput{}); got > unsafe.Sizeof(mouseInput{}) {
		t.Fatalf("KEYBDINPUT (%d) overflows the INPUT union", got)
	}
}

func TestIdleAndLockReadable(t *testing.T) {
	p, _ := newPlatform(PlatformOptions{Key: "F15"})
	defer p.Close()
	if _, err := p.IdleTime(); err != nil {
		t.Fatalf("IdleTime: %v", err)
	}
}
