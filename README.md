# Teams Refresher

Keeps Microsoft Teams showing **Available** instead of flipping to yellow
"Away" the moment you stop touching the keyboard — so you can read, think,
or work on a second machine without babysitting a key every few minutes.

Built for this box: **Ubuntu 26.04, GNOME on Wayland, Teams in Brave.**

## How it works

Teams web doesn't track your typing. It asks the browser whether *you* are
idle, and on Chromium that answer comes from the compositor's idle clock —
`org.gnome.Mutter.IdleMonitor` on GNOME/Wayland. So the whole job is: keep
that one clock from reaching five minutes.

```
   Mutter idle clock ──AddIdleWatch(180s)──▶ WatchFired signal
                                                   │
                                                   ▼
                                          screen locked?  ── yes ─▶ do nothing
                                                   │ no
                                                   ▼
                                 tap F15 on a virtual /dev/uinput keyboard
                                                   │
                                     libinput ─▶ Mutter ─▶ clock resets to 0
                                                   │
                                     Chromium Idle Detection ─▶ Teams: active
```

Three deliberate choices:

- **F15, not mouse movement.** No application binds F15, and it doesn't move
  your pointer — so it can't disturb a drag, a text selection, or a game. It
  is the quietest event that still counts as input.
- **`/dev/uinput`, not `xdotool`.** You're on Wayland. X11 tools either don't
  work or only reach XWayland clients, and neither resets Mutter's clock. A
  virtual device enters through libinput exactly like real hardware, which is
  why this works with **Brave minimised or on another workspace**.
- **Event-driven, not a polling loop.** Mutter tells us when you cross the
  idle threshold. The process sleeps the rest of the time.

It only acts when you're *actually* idle, so it never fights your real input.

## Install

```bash
./install.sh          # udev rule + 'input' group + binary + user service
# log out and back in (the group change needs a fresh session)
systemctl --user enable --now teams-refresher
```

`install.sh` needs `sudo` exactly once, to let your user create virtual input
devices. It copies the daemon to `~/.local/bin/teams-refresher` rather than
running it from this directory, so the service still starts when this
removable volume isn't mounted.

## Use

```bash
teams-refresher --status              # idle time, lock state, uinput access
teams-refresher --once                # send a single nudge, exit
teams-refresher -v --dry-run -t 10    # watch the logic with nothing emitted
journalctl --user -u teams-refresher -f

systemctl --user stop teams-refresher     # pause
systemctl --user disable --now teams-refresher   # off for good
```

### Options

| Flag | Default | Notes |
|---|---|---|
| `-t, --threshold` | `180` | Seconds idle before a nudge. Teams waits 5 min; 180s leaves headroom. |
| `-k, --key` | `F15` | `F13`–`F16` if something on your system does bind F15. |
| `--pointer` | off | Also jiggle the pointer 1px and back. Only needed for clients that watch mouse movement specifically. |
| `--allow-locked` | off | Keep nudging while the screen is locked. Off by default on purpose — see below. |
| `-n, --dry-run` | off | Log decisions, emit nothing. |
| `--status` | — | One-shot health check. |

## Behaviour worth knowing

**It stops when you lock the screen.** Locking is an explicit "I've stepped
away", so the daemon pauses and resumes on unlock. `--allow-locked` overrides
this, but then you're green while provably not at the machine — that's a
different thing from what this tool is for.

**It won't hide real absence beyond the idle clock.** If you're in a call,
Teams sets your presence from the call, not from idle. Calendar-based
statuses (In a meeting, Out of office) also win over Available.

**Your screen won't blank anyway.** `org.gnome.desktop.session idle-delay` is
already `0` on this machine, so no separate screensaver inhibitor is needed.

**Self-diagnosing.** After each nudge the daemon re-reads the idle clock. If
the clock didn't reset, it logs a warning instead of silently doing nothing —
that's your signal that the virtual device isn't reaching the compositor.

## Caveats

- **Firefox won't work this way.** It has no Idle Detection API, so Teams web
  falls back to in-page mouse/key listeners, which only see events delivered
  to the focused Teams tab. Keep Teams in Brave.
- **GNOME/Wayland specific.** The idle-clock half is Mutter's D-Bus API. The
  `uinput` half is portable to any Linux; the watch half is not.
- Uses PyGObject (`python3-gi`) and the standard library. Nothing to `pip install`.

## Files

| Path | |
|---|---|
| `teams_refresher.py` | The whole daemon (~430 lines, no dependencies). |
| `install.sh` / `uninstall.sh` | Setup and full removal. |
| `systemd/teams-refresher.service` | User unit, tied to `graphical-session.target`. |

## Troubleshooting

`--status` says uinput is **NOT writable** → you haven't logged out since
`install.sh` added you to `input`. Confirm with `id -nG | grep input`, or test
immediately without logging out: `sg input -c 'teams-refresher -v'`.

**"nudge did not reset the idle clock"** → the virtual device isn't reaching
Mutter. Check `libinput list-devices | grep -i "Teams Refresher"` while the
daemon runs.

**Still going Away** → confirm the clock actually moves: run
`watch -n1 teams-refresher --status` and check idle resets to 0 on each nudge.
If it does, Teams is deciding from something other than idle (a calendar
event, or a manually pinned status).
