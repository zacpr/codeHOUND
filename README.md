<p align="center">
  <img src="docs/images/cody-hero.png" width="100%" alt="Cody H. Ound in sunglasses and headphones: &quot;It's a normal and real name.&quot;">
</p>

<h1 align="center">codeHOUND</h1>
<p align="center"><strong>Good code. Found.</strong></p>
<p align="center">Find your projects. Remember the context. Get back to building.</p>
<p align="center">
  <a href="#install">Install</a> ·
  <a href="#the-dashboard">The dashboard</a> ·
  <a href="#configuration">Configure</a> ·
  <a href="#development">Develop</a>
</p>

That repo on the other drive. The branch you left half-finished. The promising project
with a name you can't quite remember.

**codeHOUND brings them back into view.** It finds Git repositories and loose software
projects across your disks, gathers their working state, and gives you one desktop
dashboard to search, revisit and launch them.

![codeHOUND dashboard showing featured projects, branch-aware names and a project inspector](docs/images/dashboard.png)
<p align="center"><em>The real dashboard, with illustrative project data. No private catalogue content.</em></p>

### Less archaeology. More building.

| Find your bearings | Pick up the thread | Open your tools |
| --- | --- | --- |
| Search projects, branches, paths and descriptions. Spot uncommitted work, duplicate clones and loose projects. | Five recently active projects, plus your pins. Saved summaries and last-commit context, with a fresh AI catch-up when you ask. | Launch your editor, terminal or file manager. Keep the dashboard close in the system tray. |

Built for Linux, at home on KDE/Plasma. Python + Qt, a local SQLite catalogue, and
launchers you control. **AI is optional**: finding, filtering and opening projects
works without a model. For AI features, choose a local service or a supported cloud
provider; the relevant project context is sent to that configured provider.

The hound in the corner is **Cody H. Ound**. He handles brand presence. You handle the code.

## Install

Requires Python 3.11+ and Git. The login integration uses desktop entries; scheduled
scans use a systemd user timer.

```bash
git clone https://github.com/zacpr/codeHOUND.git
cd codeHOUND
python3 -m venv .venv
.venv/bin/pip install -e .
source .venv/bin/activate

codehound app

# Optional desktop and background integration
codehound install-autostart   # app menu + start tray app at login
codehound install-timer       # systemd user timer: sync, scan + update checks every 6h
```

On first launch, open **Settings → Scan** to choose your search roots, then hit
**Scan Now**. Review **Settings → Ollama** before scanning: scan-time enrichment is
enabled by default and uses a local Ollama service if available. The separate
**Settings → AI** tab configures Ask AI and project catch-ups.

Fedora/RHEL packaging is provided in [packaging/codehound.spec](packaging/codehound.spec).

## Usage

```bash
codehound scan              # scan configured roots, update DB, enrich via Ollama
codehound scan --root PATH  # scan one root only (repeatable)
codehound sync              # clone/fetch every repo from configured accounts
codehound ask "question"    # ask the AI across your whole project catalogue
codehound ask --path P "?"  # ask about one project
codehound app               # tray icon + dashboard
codehound adopt PATH        # copy a project into ~/repos under a unique name
codehound new NAME          # create a brand-new git project in ~/repos
codehound new NAME -d "…" --scaffold  # …with AI-written starter files (falls back to README)
codehound clone SRC         # clone a git URL or local repo into ~/repos (--name to rename)
codehound worktree PATH BR  # add BR as a sibling worktree of PATH (new or existing branch)
codehound detect            # list installed editors, AI agent CLIs and local AI services
codehound open NAME         # fuzzy-find a project and open it in the first launcher
codehound janitor           # dry-run: show clones whose remote work is done
codehound janitor --force   # actually remove them
codehound updates           # list known updates for watched GitHub repos
codehound updates --check   # refresh from the GitHub API first, then list
codehound updates --activate # install everything staged (downloaded + verified)
codehound update REPO       # download + install the newest release of REPO
codehound update REPO --system     # install an RPM/DEB system-wide via pkexec
codehound update REPO --rollback   # put the previous version back
codehound self-update       # update codeHOUND itself, then relaunch on the new build
```

## The dashboard

Cody H. Ound, the codeHOUND mascot, welcomes you in the top-left sidebar, with a
charcoal, cyan and violet theme throughout the dashboard and its dialogs. His
headshot also appears in the window, taskbar, app menu and tray. RPM installs include
the themed icons; editable installs can refresh them with `codehound install-autostart`.

- **Catalogue views** — all projects, working changes, Git repositories (including bare
  repos), loose projects, duplicate clones and missing paths. All projects retains missing
  entries; other views show live paths only, except the dedicated Missing paths view.
  The overview cards are shortcuts to these views; their counts cover the entire catalogue.
  At compact window heights the cards collapse, leaving the same counts in the sidebar.
- **Continue the hunt** — a manual carousel above the table features the five most
  recently modified live projects, plus any extra projects you pin. Pin from the inspector
  or the carousel; pins persist between launches. Unpinning a top-five project still leaves
  it in the recent five. Missing pinned paths stay saved but are not featured. The
  **Featured** toolbar toggle hides the carousel when you want more room for the table.
- Each featured card shows its **saved AI description** and **Previously…**, the last
  recorded commit (not a remembered chat). **Catch me up with AI** requests a fresh
  briefing from project files and git history, shown in the usual answer dialog. Browsing
  or pinning never calls a model. The carousel covers the catalogue independently of filters.
- **Search** within the selected view with `Ctrl+F`; reload from disk with `F5`.
  Project headings in the table, inspector and carousel use `project/branch` when
  both are known. You can search this combined label; the separate Branch column remains available.
  Click headers to sort or **drag headers to rearrange columns** (order persisted).
  **Columns** exposes all ten fields, including the path, root and creation date hidden
  initially to keep the table compact. Column visibility is per-window.
- **Inspector** — select a row for its description, location and latest commit. Pick a
  launcher, open the project, ask a question, copy its path or pin it. Hide the inspector
  with its toolbar toggle to give the table more room.
- Double-click still opens the first configured launcher. Right-click retains **all
  launchers**, **Ask AI**, **Open in file manager**, **Adopt into repos…**,
  **Add worktree…** and **Copy path**.
- **New Project…** (header) opens a three-tab dialog: **New project** creates a fresh
  git repo (README + initial commit, optionally AI-scaffolded when a provider is set),
  **Clone** takes a git URL, a local path, or a catalogue project, and **Add worktree**
  branches an existing project into a sibling folder. Each runs the matching CLI command
  in the background — progress streams into **Activity**, and the new project lands in
  the catalogue without waiting for a scan.
- **Scan Now**, **Sync Accounts**, **Settings** and the tray menu remain available.
  **Activity** shows scan/sync/summary output without leaving the catalogue.

A **Status** column shows `▲ dirty` for uncommitted work — click it to sort dirty-first.

Badges on the Name column: `✱` uncommitted changes · `⧉` same remote cloned elsewhere ·
`⌂` already adopted into ~/repos. Grey rows were missing on the last scan.

## Configuration

Everything is editable in the app: **Settings** button on the dashboard (or **Settings…**
in the tray menu) — tabs for Scan (roots, excluded dirs/paths, depth, mount-point crossing),
Ollama, Launchers (add/edit/remove/reorder; the first entry is the double-click default)
and Adopt destination. The Launchers tab has **Detect installed tools…** to find
editors, file managers and AI agent CLIs on your PATH, and the AI tab has
**Detect local AI services…** to probe for running OpenAI-compatible servers.
Changes are saved to `~/.config/codehound/config.toml`, which you
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
- `[updates]` — `enabled` (watch GitHub releases of catalogued projects),
  `mode` (`stage` = download + verify, wait for your click; `silent` = install
  automatically after each check) and `self_repo` (the repo codeHOUND
  self-updates from; blank turns self-updating off)

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

## Update manager

Every catalogued project with a GitHub remote is a **watch target**: `codehound updates
--check` (or the dashboard's **Check Updates** button, or the systemd timer) asks the
GitHub API for each repo's newest published release and compares it with the local
checkout's tag. When a newer release exists, codeHOUND picks the release asset that
matches your OS and CPU architecture (AppImage/RPM/DEB/archives ranked; source archives
and foreign-arch builds skipped) and tells you — tray notification plus
`codehound updates`.

**Installing**: `codehound update OWNER/REPO` (a name fragment works too) downloads the
matched asset, verifies its SHA-256 when the publisher provides one (a `SHA256SUMS`
asset or a hash in the release notes), and installs it:

- a custom location you set with `codehound update REPO --set-path DIR` wins;
- otherwise an existing managed binary is replaced in place (old copy kept as
  `*.codehound-bak`);
- otherwise the release lands in `~/.local/share/codehound/installs/<repo>/<version>/`
  with a symlink in `~/.local/bin` (make sure it is on your PATH).

`codehound update REPO --rollback` puts the previous version back. AppImage, zip and
tar archives install user-land. For a distribution package instead,
`codehound update REPO --system` downloads and checksum-verifies the RPM/DEB and hands
it to your package manager through `pkexec` (dnf/yum/apt/dpkg/rpm, whichever is
present) — the package manager owns the files afterwards, so undo it with
`dnf downgrade` / `apt install` rather than `--rollback`.

**Automatic updates (stage-then-confirm)**: after each check, every compatible update
gets an auto policy applied — per-repo `codehound updates --auto OWNER/REPO --mode
auto|never|default` wins; otherwise the global `[updates] mode` decides. `auto` repos
(and everything, when mode is `silent`) install straight away; in `stage` mode the
release is downloaded, checksum-verified and unpacked into a staging dir but **not**
installed — the tray notification turns click-to-install, and
`codehound updates --activate` (or plain `codehound update REPO`) installs what is
staged. Staging is idempotent, and a staged release that a newer check supersedes is
refused as stale rather than installed. `codehound updates --check --no-auto` skips
the policy pass entirely.

**Updating codeHOUND itself**: codeHOUND keeps itself on the watch list via
`[updates] self_repo` (default `zacpr/codeHOUND`, blank turns it off), comparing
releases against the **installed** version rather than a checkout's tag.
`codehound self-update` (or the tray's **Update codeHOUND…**) installs its own
RPM/DEB system-wide via pkexec — the running app keeps its old files until it
exits, so the dashboard then quits and a detached helper restarts it on the new
build. Releases in other formats can't replace a running install, so they stage
for a manual swap instead. The automatic policy pass never touches the self row:
self-update is always an explicit action.

- Private repos and heavy polling need authentication: set a GitHub `token` in
  Settings → Accounts, or log in with the **gh** CLI — anonymous API access is limited
  to 60 requests/hour and private repos appear as 404s.

## How detection works

- A directory containing `.git` → **git** project (branch, remote, last/first commit, dirty
  state collected via git).
- Otherwise a directory containing a project marker (`package.json`, `pyproject.toml`,
  `Cargo.toml`, `go.mod`, `*.sln`, `CMakeLists.txt`, …) → **ungitted** project.
- Found projects are not descended into; excluded dirs, other filesystems, and hidden dirs
  are pruned.
- Everything lands in `~/.local/share/codehound/codehound.db` (SQLite). Projects that
  disappear are kept, greyed out and flagged missing.

## Development

The dashboard tests use an isolated temporary catalogue and mocked launch/AI actions:

```bash
QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests -v
```

The supplied mascot artwork lives in [codehound/assets](codehound/assets). Packaged
desktop icons use the distinct `codehound-cody` theme name so older cached icons do
not follow Cody into the new dashboard.
