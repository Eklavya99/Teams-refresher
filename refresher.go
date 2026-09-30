package main

import (
	"context"
	"time"
)

// Config is the nudge policy, independent of OS.
type Config struct {
	Threshold   time.Duration
	Key         string
	AllowLocked bool
	DryRun      bool
}

// Refresher decides when to nudge. It acts only when the user is actually
// idle past the threshold, never while the session is locked (unless told
// to), and never faster than a cooldown, so a nudge that fails to reset the
// idle clock cannot spin.
type Refresher struct {
	cfg      Config
	p        Platform
	now      func() time.Time
	cooldown time.Duration
	poll     time.Duration

	locked      bool
	nudges      int
	ineffective int
	lastNudge   time.Time // zero until the first nudge
}

func NewRefresher(cfg Config, p Platform) *Refresher {
	return &Refresher{
		cfg:      cfg,
		p:        p,
		now:      time.Now,
		cooldown: maxDur(5*time.Second, cfg.Threshold/10),
		// Backstop for a dropped watch or missed signal. Kept well under the
		// threshold so a failed watch still nudges before Teams' 5min cutoff.
		poll: maxDur(15*time.Second, cfg.Threshold/4),
	}
}

// MaybeNudge emits one activity event if it is both needed and permitted.
// It reports whether it acted (in dry-run, whether it would have).
func (r *Refresher) MaybeNudge(reason string) bool {
	if !r.lastNudge.IsZero() {
		if since := r.now().Sub(r.lastNudge); since < r.cooldown {
			logDebug("skipping nudge (%s): cooling down, %.0fs since last", reason, since.Seconds())
			return false
		}
	}

	if r.locked && !r.cfg.AllowLocked {
		logDebug("skipping nudge (%s): screen is locked", reason)
		return false
	}

	idle, err := r.p.IdleTime()
	known := err == nil
	if known && idle < r.cfg.Threshold {
		// Real activity beat us to it; nothing to do.
		logDebug("skipping nudge (%s): idle %.0fs below threshold", reason, idle.Seconds())
		return false
	}

	r.lastNudge = r.now()
	r.nudges++
	idleStr := "?"
	if known {
		idleStr = idle.Round(time.Second).String()
	}
	if r.cfg.DryRun {
		logInfo("[dry-run] would nudge #%d (%s, idle %s)", r.nudges, reason, idleStr)
		return true
	}

	if err := r.p.Nudge(); err != nil {
		logError("failed to emit nudge: %v", err)
		return false
	}
	logInfo("nudge #%d sent (%s) -- %s tapped, idle clock reset", r.nudges, reason, r.cfg.Key)
	return true
}

// Verify warns if our synthetic input is not actually resetting the clock.
// It reports whether the clock is back under the threshold.
func (r *Refresher) Verify() bool {
	idle, err := r.p.IdleTime()
	if err != nil {
		return false
	}
	if idle >= r.cfg.Threshold {
		r.ineffective++
		switch r.ineffective {
		case 1, 5, 20:
			logWarn("nudge did not reset the idle clock (still %.0fs) -- %dx now. %s",
				idle.Seconds(), r.ineffective, ineffectiveHint)
		}
		return false
	}
	if r.ineffective > 0 {
		logInfo("idle clock responding again")
	}
	r.ineffective = 0
	return true
}

func (r *Refresher) Run(ctx context.Context) error {
	if locked, err := r.p.Locked(); err == nil {
		r.locked = locked
	}
	r.p.Rearm()

	lockMode := "paused"
	if r.cfg.AllowLocked {
		lockMode = "active"
	}
	dry := ""
	if r.cfg.DryRun {
		dry = ", DRY RUN"
	}
	logInfo("watching: nudge after %s idle (backstop poll %s), key=%s, locked-screen=%s%s",
		r.cfg.Threshold, r.poll, r.cfg.Key, lockMode, dry)

	ticker := time.NewTicker(r.poll)
	defer ticker.Stop()
	events := r.p.Events()
	var verify <-chan time.Time

	nudge := func(reason string) {
		if r.MaybeNudge(reason) && !r.cfg.DryRun {
			// The OS sees the event asynchronously, so confirm shortly after.
			verify = time.After(300 * time.Millisecond)
		}
	}

	for {
		select {
		case <-ctx.Done():
			logInfo("shutting down")
			logInfo("stopped after %d nudge(s)", r.nudges)
			return nil
		case ev, ok := <-events:
			if !ok {
				events = nil
				continue
			}
			switch ev.Kind {
			case IdleCrossed:
				nudge("idle watch")
				r.p.Rearm()
			case LockChanged:
				r.locked = ev.Locked
				state, action := "unlocked", "active"
				if r.locked {
					state = "locked"
					if !r.cfg.AllowLocked {
						action = "pausing"
					}
				}
				logInfo("screen %s -- %s", state, action)
			}
		case <-ticker.C:
			r.p.Rearm()
			nudge("safety poll")
		case <-verify:
			verify = nil
			if r.Verify() {
				r.p.Rearm()
			}
		}
	}
}

func maxDur(a, b time.Duration) time.Duration {
	if a > b {
		return a
	}
	return b
}
