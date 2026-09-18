# Teams Refresher

Keeps Microsoft Teams showing **Available** instead of flipping to yellow
"Away" the moment you stop touching the keyboard — so you can read, think,
or work on a second machine without babysitting a key every few minutes.

Runs on **Linux, macOS and Windows**, with no third-party packages.

## How it works

Teams web doesn't track your typing. It asks the browser whether *you* are
idle, and the browser asks the operating system. So the whole job is: keep
that one clock from reaching five minutes.

```
   system idle clock ──── past the threshold? ────▶ screen locked? ── yes ─▶ do nothing
            ▲                                             │ no
            │                                             ▼
            │                              tap F15 on a synthetic keyboard
            │                                             │
            └────────── clock resets to 0 ◀───────────────┘
                                 │
                   browser idle detection ─▶ Teams: active
```

Every operating system spells both halves differently, so each one gets its
own backend and the loop above stays the same:

| | reset the clock | read the clock | detect the lock |
|---|---|---|---|
| **Linux** | `/dev/uinput` virtual device (XTEST on X11 as a fallback) | Mutter on GNOME, freedesktop on KDE, XScreenSaver on X11 | GNOME / freedesktop ScreenSaver |
| **macOS** | `CGEventPost` | `CGEventSourceSecondsSinceLastEventType` | `CGSSessionScreenIsLocked` |
| **Windows** | `SendInput` | `GetLastInputInfo` | `OpenInputDesktop` |

Three deliberate choices, unchanged from the original Linux-only version:

- **F15, not mouse movement.** No application binds F15, and it doesn't move
  your pointer — so it can't disturb a drag, a text selection, or a game. It
  is the quietest event that still counts as input.
- **Synthetic input at the system level, not a browser automation trick.**
  The event enters the input stack the way real hardware does, which is why
  this works with **the browser minimised or on another workspace**.
- **It only acts when you're actually idle.** It never fights your real
  input, and it stops entirely when you lock the screen.

Instead of a fixed poll, the daemon sleeps until the earliest moment the
threshold could next be crossed — 12s idle against a 180s threshold means
nothing can happen for 168s, so it sleeps 168s. While you're working it backs
off to almost nothing.

## Install

The same two commands everywhere:

```bash
pip install --user .
teams-refresher --install-service     # start it at every login
```

`--install-service` writes whichever of these your system uses, baking in any
other options you pass alongside it:

| | |
|---|---|
| Linux | a systemd user unit in `~/.config/systemd/user/` |
| macOS | a LaunchAgent in `~/Library/LaunchAgents/` |
| Windows | an entry under `HKCU\...\CurrentVersion\Run` |

Install the package properly (rather than running it out of a clone) if you
want the login service to survive: the service records the path it was
installed from, and a checkout on a removable drive won't be there at boot.

### One extra step per platform

**Linux** — creating a virtual input device is privileged. Run the helper
once; it adds a udev rule and puts you in the `input` group:

```bash
./install.sh
# log out and back in, so the group change takes effect
```

This is the only part that needs `sudo`, and it's the only reason the daemon
can work under Wayland at all: X11 tools like `xdotool` either don't work or
reach only XWayland clients, and neither resets the compositor's clock.

**macOS** — grant Accessibility permission to whatever runs the daemon
(your terminal for a foreground run, the `python3` binary for the LaunchAgent):
System Settings → Privacy & Security → Accessibility. macOS ties the grant to
the exact binary, so re-grant it after upgrading Python. The daemon refuses to
start with instructions rather than tapping into the void.

**Windows** — nothing. A normal user process may synthesise input into its own
session.

## Use

```bash
teams-refresher --status              # backends in use, idle time, lock state
teams-refresher --once                # send a single nudge, exit
teams-refresher -v --dry-run -t 10    # watch the logic with nothing emitted
teams-refresher                       # run in the foreground, Ctrl-C to stop
```

Watching the service:

```bash
journalctl --user -u teams-refresher -f          # Linux
tail -f ~/Library/Logs/teams-refresher.log       # macOS
type %LOCALAPPDATA%\teams-refresher\teams-refresher.log   :: Windows
```

Turning it off:

```bash
teams-refresher --uninstall-service
```

### Options

| Flag | Default | Notes |
|---|---|---|
| `-t, --threshold` | `180` | Seconds idle before a nudge. Teams waits 5 min; 180s leaves headroom. |
| `-k, --key` | `F15` | `F13`–`F16` if something on your system does bind F15. |
| `--pointer` | off | Also jiggle the pointer 1px and back. Only for clients that watch mouse movement specifically. |
| `--allow-locked` | off | Keep nudging while the screen is locked. Off by default on purpose — see below. |
| `-n, --dry-run` | off | Log decisions, emit nothing. |
| `--log-file` | — | Append to a rotating log as well as stderr. |
| `--status` | — | One-shot health check. |
| `--install-service` / `--uninstall-service` / `--service-status` | — | Login service, per platform. |

## Behaviour worth knowing

**It stops when you lock the screen.** Locking is an explicit "I've stepped
away", so the daemon pauses and resumes on unlock. `--allow-locked` overrides
this, but then you're green while provably not at the machine — that's a
different thing from what this tool is for.

**It won't hide real absence beyond the idle clock.** If you're in a call,
Teams sets your presence from the call, not from idle. Calendar-based statuses
(In a meeting, Out of office) also win over Available.

**Self-diagnosing.** After each nudge the daemon re-reads the idle clock. If
the clock didn't reset, it logs a warning instead of silently doing nothing —
that's your signal that the synthetic input isn't reaching the compositor.

**It degrades rather than refusing.** A system whose idle clock it can't read
falls back to nudging on a fixed cadence; one whose lock state it can't read
simply never pauses. `--status` always says which backend was chosen.

## Caveats

- **Use a Chromium browser** (Chrome, Edge, Brave) for Teams web. Firefox has
  no Idle Detection API, so Teams falls back to in-page listeners that only
  see events delivered to the focused tab — no synthetic system-level input
  will reach it. The Teams desktop app reads the same system idle clock as
  Chromium and works fine.
- **Linux/Wayland needs `uinput`.** On X11 the daemon can fall back to XTEST,
  but a Wayland compositor ignores XTEST entirely.
- **Windows: it can't run as a Windows service.** A real service runs in
  session 0, where its input would never reach your desktop. The Run key entry
  runs as you, in your session, which is what this needs.
- Python 3.9 or newer. PyGObject is used on GNOME when it happens to be
  installed, and `gdbus` is used when it isn't; neither is required to install.

## Files

| Path | |
|---|---|
| `teams_refresher/core.py` | The watch loop. No OS-specific code. |
| `teams_refresher/backends/base.py` | The four interfaces a platform fills in. |
| `teams_refresher/backends/{linux,macos,windows}.py` | One per platform, all `ctypes` and standard library. |
| `teams_refresher/cli.py` | Arguments, `--status`, login-service commands. |
| `install.sh` / `uninstall.sh` | Linux `uinput` permissions only. |

## Troubleshooting

Start with `teams-refresher --status`. It prints the backend chosen for each
job and whether the injector can actually open.

**Linux — "Cannot open /dev/uinput"** → you haven't logged out since
`install.sh` added you to `input`. Confirm with `id -nG | grep input`, or test
without logging out: `sg input -c 'teams-refresher -v'`.

**macOS — "macOS is refusing synthetic input"** → Accessibility permission,
as above. After a Python upgrade, remove the old entry and add it again.

**"nudge did not reset the idle clock"** → the synthetic input isn't reaching
the clock. On Linux, check `libinput list-devices | grep -i "Teams Refresher"`
while the daemon runs. On macOS this is almost always Accessibility.

**Still going Away** → confirm the clock actually moves: run
`teams-refresher --status` twice around a `teams-refresher --once` and check
that idle drops to ~0. If it does, Teams is deciding from something other than
idle (a calendar event, or a manually pinned status).
