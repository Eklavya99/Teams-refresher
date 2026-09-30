// Command teams-refresher keeps Microsoft Teams from flipping to "Away" while
// you're at the machine.
//
// Teams decides you're idle from the OS idle clock. This daemon watches that
// clock and, once it passes a threshold, taps a no-op key (F15 by default) so
// the clock resets and Teams keeps seeing you as active. No application binds
// F15 and it doesn't move the pointer, so the nudge is invisible.
//
// It does nothing while you're actually using the machine, and nothing at all
// while the screen is locked (locking is an explicit "I've stepped away").
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"os/signal"
	"runtime"
	"slices"
	"strings"
	"syscall"
	"time"
)

type options struct {
	threshold   int
	key         string
	pointer     bool
	allowLocked bool
	once        bool
	dryRun      bool
	verbose     bool
	status      bool
	install     bool
	uninstall   bool
}

func parseFlags(args []string) (*options, error) {
	o := &options{}
	fs := flag.NewFlagSet("teams-refresher", flag.ContinueOnError)
	for _, name := range []string{"t", "threshold"} {
		fs.IntVar(&o.threshold, name, 180, "")
	}
	for _, name := range []string{"k", "key"} {
		fs.StringVar(&o.key, name, "F15", "")
	}
	for _, name := range []string{"n", "dry-run"} {
		fs.BoolVar(&o.dryRun, name, false, "")
	}
	for _, name := range []string{"v", "verbose"} {
		fs.BoolVar(&o.verbose, name, false, "")
	}
	fs.BoolVar(&o.pointer, "pointer", false, "")
	fs.BoolVar(&o.allowLocked, "allow-locked", false, "")
	fs.BoolVar(&o.once, "once", false, "")
	fs.BoolVar(&o.status, "status", false, "")
	fs.BoolVar(&o.install, "install", false, "")
	fs.BoolVar(&o.uninstall, "uninstall", false, "")
	fs.Usage = func() { fmt.Fprint(fs.Output(), usage()) }

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	if fs.NArg() > 0 {
		return nil, fmt.Errorf("unexpected argument: %s", fs.Arg(0))
	}
	o.key = strings.ToUpper(o.key)
	if !slices.Contains(Keys, o.key) {
		return nil, fmt.Errorf("--key must be one of %s", strings.Join(Keys, ", "))
	}
	if o.threshold < 1 {
		return nil, errors.New("--threshold must be at least 1 second")
	}
	return o, nil
}

func usage() string {
	s := `Hold Microsoft Teams "Available" by resetting the OS idle clock when you go idle.

Usage: teams-refresher [options]

  -t, --threshold SECONDS  idle time before a nudge (default 180, comfortably
                           inside the 5 minutes Teams waits before going Away)
  -k, --key KEY            which no-op key to tap: F13, F14, F15, F16 (default F15)
      --pointer            also jiggle the pointer 1px and back, for clients
                           that only watch mouse movement
      --allow-locked       keep nudging even when the screen is locked
      --once               send a single nudge and exit (useful for testing)
  -n, --dry-run            log what would happen without emitting any input
  -v, --verbose            debug logging
      --status             print current idle time and lock state, then exit
`
	if runtime.GOOS == "windows" {
		s += `      --install            copy to %LOCALAPPDATA%\Programs, start at login, start now
                           (other options given here are kept for the login copy)
      --uninstall          stop it and remove the login entry
`
	}
	return s
}

func main() {
	os.Exit(run(os.Args[1:]))
}

func run(args []string) int {
	o, err := parseFlags(args)
	if errors.Is(err, flag.ErrHelp) {
		return 0
	}
	if err != nil {
		fmt.Fprintf(os.Stderr, "error: %v\n\n%s", err, usage())
		return 2
	}

	daemon := !(o.status || o.once || o.install || o.uninstall)
	prepareOutput(daemon)
	logVerbose = o.verbose

	cfg := Config{
		Threshold:   time.Duration(o.threshold) * time.Second,
		Key:         o.key,
		AllowLocked: o.allowLocked,
		DryRun:      o.dryRun,
	}

	switch {
	case o.install:
		return report(installService(cfg, o.pointer))
	case o.uninstall:
		return report(uninstallService())
	case o.status:
		return status(cfg)
	}

	if o.once && o.dryRun {
		logInfo("[dry-run] would tap %s once", o.key)
		return 0
	}

	p, err := newPlatform(PlatformOptions{
		Key:       o.key,
		Pointer:   o.pointer,
		Threshold: cfg.Threshold,
		Input:     !o.dryRun,
		Watch:     !o.once,
	})
	if err != nil {
		return report(err)
	}
	defer p.Close()

	if o.once {
		if err := p.Nudge(); err != nil {
			return report(err)
		}
		logInfo("single %s nudge sent", o.key)
		return 0
	}

	release, ok := singleInstance()
	if !ok {
		logInfo("another teams-refresher is already running; exiting")
		return 0
	}
	defer release()

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	return report(NewRefresher(cfg, p).Run(ctx))
}

func status(cfg Config) int {
	p, err := newPlatform(PlatformOptions{Key: cfg.Key, Threshold: cfg.Threshold})
	if err != nil {
		return report(err)
	}
	defer p.Close()

	idle := "unknown"
	if d, err := p.IdleTime(); err == nil {
		idle = fmt.Sprintf("%.1fs", d.Seconds())
	}
	locked := "unknown"
	if l, err := p.Locked(); err == nil {
		locked = fmt.Sprint(l)
	}
	fmt.Printf("idle:   %s\n", idle)
	fmt.Printf("locked: %s\n", locked)
	for _, line := range p.StatusLines() {
		fmt.Println(line)
	}
	return 0
}

func report(err error) int {
	if err == nil {
		return 0
	}
	// logOut is stderr, or the log file when running detached on Windows.
	fmt.Fprintf(logOut, "error: %v\n", err)
	if errors.Is(err, ErrPermission) {
		return 13
	}
	return 1
}
