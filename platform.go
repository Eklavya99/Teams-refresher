package main

import (
	"errors"
	"time"
)

// Platform is everything OS-specific: reading the idle clock, knowing whether
// the session is locked, and injecting one invisible input event. The nudge
// policy in refresher.go is shared and knows nothing about how these work.
type Platform interface {
	// IdleTime is how long since the last user input, as the OS sees it.
	IdleTime() (time.Duration, error)
	// Locked reports whether the session is locked right now.
	Locked() (bool, error)
	// Nudge emits one activity event (a no-op key tap, optionally a
	// round-trip pointer move).
	Nudge() error
	// Events delivers idle-threshold crossings and lock changes as they
	// happen. The refresher also runs a backstop poll, so events are an
	// accelerator, not a requirement.
	Events() <-chan Event
	// Rearm re-establishes any idle watch that fired or was lost. Safe to
	// call often; a no-op where the OS has no watch mechanism.
	Rearm()
	// StatusLines are extra platform-specific lines for --status.
	StatusLines() []string
	Close() error
}

type EventKind int

const (
	IdleCrossed EventKind = iota
	LockChanged
)

type Event struct {
	Kind   EventKind
	Locked bool // for LockChanged
}

// PlatformOptions configures newPlatform (implemented per OS).
type PlatformOptions struct {
	Key       string        // F13..F16
	Pointer   bool          // also jiggle the pointer
	Threshold time.Duration // idle time that counts as "crossed"
	Input     bool          // create the input injector (false for --status / dry-run)
	Watch     bool          // start delivering Events
}

// ErrPermission marks a failure the user fixes by granting access, so main can
// exit with the same status code (13) the Python version used.
var ErrPermission = errors.New("permission denied")

// Keys is the set of no-op keys we are willing to tap.
var Keys = []string{"F13", "F14", "F15", "F16"}
