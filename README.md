# Teams Refresher

Keeps Microsoft Teams showing **Available** instead of flipping to yellow
"Away" the moment you stop touching the keyboard — so you can read, think,
or work on a second machine without babysitting a key every few minutes.

One small Go program, shipped as a single file per OS with nothing else to
install:

| OS | Works with | File |
|---|---|---|
| **Windows 10/11** | New Teams desktop app, or Teams in Edge/Chrome | `teams-refresher-windows-amd64.exe` (`-arm64` for Snapdragon PCs) |
| **Linux** (GNOME on Wayland) | Teams in a Chromium browser (Brave, Chrome, Edge) | `teams-refresher-linux-amd64` / `-arm64` |

## How it works

Teams doesn't track your typing. It asks the OS (or, on the web, the browser,
which asks the OS) how long since the last input. So the whole job is: keep
that one idle clock from reaching five minutes.

```
   OS idle clock ──passes 180s──▶ screen locked?  ── yes ─▶ do nothing
                                        │ no
                                        ▼
                              tap F15 as a real key event
                                        │
                              idle clock resets to 0 ─▶ Teams: active
```

- **Windows:** reads the idle clock with `GetLastInputInfo` (checked every
  5s, a single cheap call) and taps the key with `SendInput`. No admin
  rights, no driver.
- **Linux:** asks Mutter (`org.gnome.Mutter.IdleMonitor`) to signal the moment
  the clock passes the threshold — event-driven, no polling loop — and taps
  the key on a virtual keyboard created through `/dev/uinput`. That enters
  through libinput exactly like real hardware, so it works with the browser
  minimised or on another workspace. X11 tools like `xdotool` don't reset
  Mutter's clock on Wayland; this does.

Deliberate choices, both platforms:

- **F15, not mouse movement.** No application binds F15, and it doesn't move
  your pointer — so it can't disturb a drag, a text selection, or a game. It
  is the quietest event that still counts as input.
- **Only when you're actually idle.** It never fights your real input, and
  after each nudge it re-reads the idle clock and warns if the nudge didn't
  register, instead of silently doing nothing.

## Install — Windows

1. Download `teams-refresher-windows-amd64.exe` from the
   [latest release](https://github.com/eklavya99/teams-refresher/releases/latest)
   (or from the **Actions** tab → latest CI run → `teams-refresher` artifact).
2. Open a terminal (PowerShell or cmd) in your Downloads folder and install it:
   ```powershell
   .\teams-refresher-windows-amd64.exe --install
   ```
   This copies it to `%LOCALAPPDATA%\Programs\teams-refresher\`, registers it
   to start at login (per-user `Run` key — no admin, no scheduled task), and
   starts it now in the background.

   The exe isn't code-signed, so Windows SmartScreen may warn the first time:
   **More info → Run anyway**.

Try it before installing, in the foreground with a 10s threshold:
```powershell
.\teams-refresher-windows-amd64.exe -v -t 10
```
Leave the keyboard alone and you'll see a nudge logged every ~10s. `Ctrl+C`
stops it.

Manage the installed copy:
```powershell
$tr = "$env:LOCALAPPDATA\Programs\teams-refresher\teams-refresher.exe"
& $tr --status                                         # idle time, lock state
Get-Content "$env:LOCALAPPDATA\teams-refresher\teams-refresher.log" -Wait   # live log
& $tr --uninstall                                      # stop + remove login entry
```
Options passed to `--install` (e.g. `--install -t 120 --pointer`) are kept
for the login copy. To change them, run `--install` again.

## Install — Linux

```bash
./install.sh          # udev rule + 'input' group + binary + user service
# log out and back in (the group change needs a fresh session)
systemctl --user enable --now teams-refresher
```

`install.sh` gets the binary in this order: an executable named
`teams-refresher` already in this folder, else `go build` if Go ≥ 1.22 is
installed, else the latest release download. It needs `sudo` exactly once,
to let your user create virtual input devices. It installs the daemon to
`~/.local/bin/teams-refresher` rather than running it from this directory, so
the service still starts when this volume isn't mounted.

```bash
teams-refresher --status                  # idle time, lock state, uinput access
systemctl --user status teams-refresher   # is the service running?
journalctl --user -u teams-refresher -f   # watch it work
systemctl --user disable --now teams-refresher   # off for good
./uninstall.sh                            # remove everything install.sh added
```

## Options

| Flag | Default | Notes |
|---|---|---|
| `-t, --threshold` | `180` | Seconds idle before a nudge. Teams waits 5 min; 180s leaves headroom. |
| `-k, --key` | `F15` | `F13`–`F16` if something on your system does bind F15. |
| `--pointer` | off | Also jiggle the pointer 1px and back. Only needed for clients that watch mouse movement specifically. |
| `--allow-locked` | off | Keep nudging while the screen is locked. Off by default on purpose — see below. |
| `--once` | — | Send a single nudge and exit. |
| `-n, --dry-run` | off | Log decisions, emit nothing. |
| `-v, --verbose` | off | Debug logging. |
| `--status` | — | One-shot health check. |
| `--install` / `--uninstall` | — | Windows only. Linux uses `install.sh` / `uninstall.sh`. |

## Behaviour worth knowing

**It stops when you lock the screen.** Locking (Win+L, or GNOME's lock) is
an explicit "I've stepped away", so the daemon pauses and resumes on unlock.
`--allow-locked` overrides this, but then you're green while provably not at
the machine — that's a different thing from what this tool is for.

**Windows: it also holds off the automatic screen lock and screensaver.**
Those run off the same idle clock, so while this is running your PC won't
lock itself after N minutes of inactivity. Lock manually with **Win+L** when
you walk away. If your organisation requires an inactivity lock, check before
using this on a work machine.

**It won't hide real absence beyond the idle clock.** If you're in a call,
Teams sets your presence from the call, not from idle. Calendar-based
statuses (In a meeting, Out of office) and a manually set status also win
over Available.

**Linux: your screen won't blank anyway** if
`org.gnome.desktop.session idle-delay` is `0`; otherwise the nudges keep it
awake too, the same as on Windows.

## Caveats

- **Linux + Firefox won't work.** Firefox has no Idle Detection API, so
  Teams web falls back to in-page listeners that only see events delivered to
  the focused Teams tab. Use a Chromium browser. (On Windows, the browser
  doesn't matter.)
- **Linux is GNOME/Wayland specific.** The idle-watch half is Mutter's D-Bus
  API; the `uinput` half would work on any Linux.
- **Windows: elevated windows.** `SendInput` can't reach a window running as
  administrator while it has focus. The daemon warns in its log if a nudge
  didn't reset the idle clock.

## Build from source

```bash
go test ./...
go build .            # for this OS
./build.sh            # all four release binaries into dist/
```

Pushing a `v*` tag runs `.github/workflows/release.yml`, which builds the
binaries and attaches them to a GitHub Release.

## Files

| Path | |
|---|---|
| `main.go` | Flags, modes (`--status`, `--once`, daemon). |
| `refresher.go` | The nudge policy, shared by all OSes. |
| `platform_linux.go` | Mutter D-Bus idle watch + `/dev/uinput` keyboard. |
| `platform_windows.go` | `GetLastInputInfo` + `SendInput`, lock detection. |
| `install_windows.go` | `--install` / `--uninstall`, background logging. |
| `install.sh` / `uninstall.sh` | Linux setup and full removal. |
| `teams-refresher.service` | systemd user unit, tied to `graphical-session.target`. |

## Troubleshooting

**Linux: `--status` says uinput is NOT writable** → you haven't logged out
since `install.sh` added you to `input`. Confirm with `id -nG | grep input`,
or test immediately without logging out: `sg input -c 'teams-refresher -v'`.

**"nudge did not reset the idle clock"** → the event isn't reaching the OS.
On Linux, check `libinput list-devices | grep -i "Teams Refresher"` while the
daemon runs. On Windows, check whether an elevated app had focus, or try
`--pointer`.

**Still going Away** → confirm the clock actually moves: run `--status` a few
times while idle and check idle resets to ~0 after each nudge. If it does,
Teams is deciding from something other than idle (a calendar event, or a
manually pinned status).

**Windows: nothing seems to run after `--install`** → look at the log in
`%LOCALAPPDATA%\teams-refresher\teams-refresher.log`, and check Task
Manager → Startup apps that `teams-refresher` is enabled.
