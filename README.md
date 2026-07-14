# codeHOUND 🐶

Hunts down git repos and stray software projects scattered across your disks, and puts them
in a sortable tray-app dashboard from which you can launch each project in Dolphin, VS Code,
Antigravity, Kimi CLI, Konsole — or anything else you configure.

## Install

```bash
cd ~/repos/codeHOUND
python3 -m venv .venv
.venv/bin/pip install -e .

.venv/bin/codehound install-autostart   # app menu + start tray app at login
.venv/bin/codehound install-timer       # systemd user timer: scan every 6h
```

## Usage

```bash
codehound scan              # scan configured roots, update DB, enrich via Ollama
codehound scan --root PATH  # scan one root only (repeatable)
codehound app               # tray icon + dashboard
codehound adopt PATH        # copy a project into ~/repos under a unique name
```

Dashboard: type to filter, click headers to sort, double-click a row to open in the first
launcher, right-click for all launchers + **Adopt into repos…**

Badges on the Name column: `✱` uncommitted changes · `⧉` same remote cloned elsewhere ·
`⌂` already adopted into ~/repos. Grey rows were missing on the last scan.

## Configuration

`~/.config/codehound/config.toml` (created with defaults on first run):

- `[scan] roots` — directories to hunt; add/remove as needed
- `[scan] exclude_dirs` / `exclude_paths` / `max_depth` / `one_filesystem`
- `[ollama]` — `enabled`, `model` (default `gemma4:12b`, auto-pulled), `enrich`
  (one-line AI descriptions), `classify` (LLM decides if marker-less dirs holding loose
  code are projects)
- `[launchers]` — `name = "command {path}"`, shown in order in the context menu; the first
  one is the double-click default
- `[adopt] dest` — where adopted projects are copied (default `~/repos`)

## How detection works

- A directory containing `.git` → **git** project (branch, remote, last/first commit, dirty
  state collected via git).
- Otherwise a directory containing a project marker (`package.json`, `pyproject.toml`,
  `Cargo.toml`, `go.mod`, `*.sln`, `CMakeLists.txt`, …) → **ungitted** project.
- Found projects are not descended into; excluded dirs, other filesystems, and hidden dirs
  are pruned.
- Everything lands in `~/.local/share/codehound/codehound.db` (SQLite). Projects that
  disappear are kept, greyed out and flagged missing.
