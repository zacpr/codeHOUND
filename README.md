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
codehound sync              # clone/fetch every repo from configured accounts
codehound ask "question"    # ask the AI across your whole project catalogue
codehound ask --path P "?"  # ask about one project
codehound app               # tray icon + dashboard
codehound adopt PATH        # copy a project into ~/repos under a unique name
codehound open NAME         # fuzzy-find a project and open it in the first launcher
codehound janitor           # dry-run: show clones whose remote work is done
codehound janitor --force   # actually remove them
```

Dashboard: type to filter, click headers to sort, **drag headers to rearrange columns**
(persisted), double-click a row to open in the first launcher, right-click for all
launchers + **Ask AI** + **Open in file manager** + **Adopt into repos…**. A **Status**
column shows `▲ dirty` for uncommitted work — click it to sort dirty-first.

Dashboard: type to filter, click headers to sort, double-click a row to open in the first
launcher, right-click for all launchers + **Ask AI** (summarise / what's it up to? / custom
question) + **Adopt into repos…**

Badges on the Name column: `✱` uncommitted changes · `⧉` same remote cloned elsewhere ·
`⌂` already adopted into ~/repos. Grey rows were missing on the last scan.

## Configuration

Everything is editable in the app: **Settings** button on the dashboard (or **Settings…**
in the tray menu) — tabs for Scan (roots, excluded dirs/paths, depth, mount-point crossing),
Ollama, Launchers (add/edit/remove/reorder; the first entry is the double-click default)
and Adopt destination. Changes are saved to `~/.config/codehound/config.toml`, which you
can also edit by hand (created with defaults on first run):

- `[scan] roots` — directories to hunt; add/remove as needed
- `[scan] exclude_dirs` / `exclude_paths` / `max_depth` / `one_filesystem`
- `[ollama]` — `enabled`, `model` (default `gemma4:12b`, auto-pulled), `enrich`
  (one-line AI descriptions), `classify` (LLM decides if marker-less dirs holding loose
  code are projects)
- `[launchers]` — `name = "command {path}"`, shown in order in the context menu; the first
  one is the double-click default
- `[adopt] dest` — where adopted projects are copied (default `~/repos`)
- `[clone] dest` — where synced account repos live, laid out as
  `<dest>/<host>/<owner>/<repo>` (default `~/repos`, so they show up in the dashboard)
- `[[accounts.github]]` — `user` (label), `token` (PAT; blank = use the **gh** CLI login),
  `orgs` (also mirror these), `forks`, `archived`
- `[[accounts.azure]]` — `org`, `pat` (blank = use the **az** CLI login),
  `projects` (blank = all projects in the org)
- `[ai]` — model connection for `codehound ask` / the dashboard's Ask AI actions:
  `provider` (`ollama` local by default, or `openai`, `gemini`, `kimi`, `custom`),
  `model` / `base_url` (blank = provider default), `api_key` (blank = env var:
  `OPENAI_API_KEY`, `GEMINI_API_KEY`, `MOONSHOT_API_KEY`), and `catalogue_summary`
  (see below)

## Catalogue summaries

With `[ai] catalogue_summary = true` (Settings → AI), the model writes a compact digest
of the catalogue after each scan (`codehound ask --refresh-digest` to do it by hand).
Catalogue questions then answer from the digest plus a compact name/path appendix —
one model call, no re-feeding of the whole project list. Freshness comes for free from
the scanner: each scan refreshes every project's `modified` stamp from directory mtimes,
so the digest is flagged **stale** (CLI note, Ask AI tooltip/menu, tray) as soon as any
project's folder changes after it was written — no file watcher needed.

## Account mirroring

`codehound sync` lists every repo in your configured GitHub / Azure DevOps accounts,
clones the missing ones, and fast-forwards the rest (`git fetch --all --prune` +
`git pull --ff-only`). Clones with uncommitted or diverged work are fetched but never
pulled, so local work is never clobbered. The systemd timer runs `sync` before each scan.

Providers are modular: `codehound/providers/` holds one module per host behind a common
`Provider` interface with pluggable auth backends (`gh` CLI, PAT, `az` CLI, bearer
headers) — adding GitLab etc. is one new module plus a registry entry. AI chat works the
same way via `codehound/ai/`: any OpenAI-compatible endpoint is a one-line preset.

## Janitor

`codehound janitor` (dry-run by default) asks each configured provider which repos are
finished — **GitHub**: archived repos; **Azure DevOps**: disabled repos and branches whose
PRs are completed or abandoned. A clone is only listed (or removed, with `--force`) when
it has no uncommitted changes and no unpushed commits. Settings → Janitor has the toggle,
an exclude list (repo names or full paths), and a dry-run button.

## How detection works

- A directory containing `.git` → **git** project (branch, remote, last/first commit, dirty
  state collected via git).
- Otherwise a directory containing a project marker (`package.json`, `pyproject.toml`,
  `Cargo.toml`, `go.mod`, `*.sln`, `CMakeLists.txt`, …) → **ungitted** project.
- Found projects are not descended into; excluded dirs, other filesystems, and hidden dirs
  are pruned.
- Everything lands in `~/.local/share/codehound/codehound.db` (SQLite). Projects that
  disappear are kept, greyed out and flagged missing.
