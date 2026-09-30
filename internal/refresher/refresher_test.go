package refresher

import (
	"errors"
	"io"
	"testing"
	"time"

	"github.com/eklavya99/teams-refresher/internal/logx"
	"github.com/eklavya99/teams-refresher/internal/platform"
)

type fakePlatform struct {
	idle    time.Duration
	idleErr error
	nudges  int
	nudgeFn func(*fakePlatform) // runs on each Nudge, e.g. to reset idle
}

func (f *fakePlatform) IdleTime() (time.Duration, error) { return f.idle, f.idleErr }
func (f *fakePlatform) Locked() (bool, error)            { return false, nil }
func (f *fakePlatform) Nudge() error {
	f.nudges++
	if f.nudgeFn != nil {
		f.nudgeFn(f)
	}
	return nil
}
func (f *fakePlatform) Events() <-chan platform.Event { return nil }
func (f *fakePlatform) Rearm()                        {}
func (f *fakePlatform) StatusLines() []string         { return nil }
func (f *fakePlatform) Close() error                  { return nil }

func init() { logx.Out = io.Discard }

func newTest(cfg Config, p *fakePlatform) (*Refresher, *time.Time) {
	if cfg.Threshold == 0 {
		cfg.Threshold = 180 * time.Second
	}
	if cfg.Key == "" {
		cfg.Key = "F15"
	}
	r := New(cfg, p)
	clock := time.Date(2026, 1, 1, 9, 0, 0, 0, time.UTC)
	r.now = func() time.Time { return clock }
	return r, &clock
}

func TestNudgesWhenIdlePastThreshold(t *testing.T) {
	p := &fakePlatform{idle: 200 * time.Second}
	r, _ := newTest(Config{}, p)
	if !r.MaybeNudge("test") || p.nudges != 1 {
		t.Fatalf("expected one nudge, got %d", p.nudges)
	}
}

func TestSkipsWhenUserActive(t *testing.T) {
	p := &fakePlatform{idle: 10 * time.Second}
	r, _ := newTest(Config{}, p)
	if r.MaybeNudge("test") || p.nudges != 0 {
		t.Fatalf("nudged while user active")
	}
}

func TestNudgesWhenIdleUnknown(t *testing.T) {
	// Matches the original: if the idle clock can't be read, err on the side
	// of nudging (the cooldown still bounds the rate).
	p := &fakePlatform{idleErr: errors.New("no bus")}
	r, _ := newTest(Config{}, p)
	if !r.MaybeNudge("test") {
		t.Fatalf("expected nudge when idle is unknown")
	}
}

func TestCooldown(t *testing.T) {
	p := &fakePlatform{idle: 200 * time.Second}
	r, clock := newTest(Config{}, p) // cooldown = max(5s, 18s) = 18s
	r.MaybeNudge("first")
	*clock = clock.Add(10 * time.Second)
	if r.MaybeNudge("too soon") {
		t.Fatalf("nudged inside cooldown")
	}
	*clock = clock.Add(10 * time.Second)
	if !r.MaybeNudge("after cooldown") || p.nudges != 2 {
		t.Fatalf("expected second nudge after cooldown, got %d", p.nudges)
	}
}

func TestLocked(t *testing.T) {
	p := &fakePlatform{idle: 200 * time.Second}
	r, _ := newTest(Config{}, p)
	r.locked = true
	if r.MaybeNudge("locked") || p.nudges != 0 {
		t.Fatalf("nudged while locked")
	}

	r2, _ := newTest(Config{AllowLocked: true}, p)
	r2.locked = true
	if !r2.MaybeNudge("allow-locked") || p.nudges != 1 {
		t.Fatalf("--allow-locked should nudge while locked")
	}
}

func TestDryRunEmitsNothing(t *testing.T) {
	p := &fakePlatform{idle: 200 * time.Second}
	r, _ := newTest(Config{DryRun: true}, p)
	if !r.MaybeNudge("dry") {
		t.Fatalf("dry-run should report it would nudge")
	}
	if p.nudges != 0 {
		t.Fatalf("dry-run emitted input")
	}
}

func TestVerifyCountsIneffectiveNudges(t *testing.T) {
	p := &fakePlatform{idle: 200 * time.Second}
	r, _ := newTest(Config{}, p)
	for i := 0; i < 3; i++ {
		if r.Verify() {
			t.Fatalf("verify passed with idle still high")
		}
	}
	if r.ineffective != 3 {
		t.Fatalf("ineffective = %d, want 3", r.ineffective)
	}
	p.idle = 0
	if !r.Verify() || r.ineffective != 0 {
		t.Fatalf("verify should pass and reset the counter once idle drops")
	}
}

func TestIntervals(t *testing.T) {
	r, _ := newTest(Config{Threshold: 10 * time.Second}, &fakePlatform{})
	if r.cooldown != 5*time.Second || r.poll != 15*time.Second {
		t.Fatalf("small threshold: cooldown %s poll %s", r.cooldown, r.poll)
	}
	r, _ = newTest(Config{Threshold: 600 * time.Second}, &fakePlatform{})
	if r.cooldown != 60*time.Second || r.poll != 150*time.Second {
		t.Fatalf("large threshold: cooldown %s poll %s", r.cooldown, r.poll)
	}
}
