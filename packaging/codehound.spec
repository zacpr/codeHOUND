Name:           codehound
Version:        0.5.1.4
Release:        1%{?dist}
Summary:        Repo hunter and launcher dashboard

License:        MIT
URL:            https://github.com/zacpr/codeHOUND
Source0:        %{name}-%{version}.tar.gz

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  python3-pip
BuildRequires:  python3-wheel
Requires:       python3 >= 3.11
Requires:       python3-pyside6
Requires:       git

%description
Hunts down git repos and stray software projects across your disks;
tray dashboard to find, launch, and organise them. Syncs GitHub and
Azure DevOps accounts, asks AI about your catalogue, and cleans up
finished work.

%prep
%autosetup

%build
# nothing to compile — pure Python

%install
# install the package into site-packages
python3 -m pip install --root %{buildroot} --no-deps --no-build-isolation .

# wrapper script
mkdir -p %{buildroot}%{_bindir}
cat > %{buildroot}%{_bindir}/codehound << 'EOF'
#!/usr/bin/python3
from codehound.cli import main
import sys
sys.exit(main())
EOF
chmod 755 %{buildroot}%{_bindir}/codehound

# desktop file
mkdir -p %{buildroot}%{_datadir}/applications
install -m 644 codehound/assets/codehound.desktop %{buildroot}%{_datadir}/applications/

# icon
for size in 16 22 24 32 48 64 128 256; do
    mkdir -p %{buildroot}%{_datadir}/icons/hicolor/${size}x${size}/apps
    install -m 644 codehound/assets/icons/${size}x${size}/apps/codehound.png \
        %{buildroot}%{_datadir}/icons/hicolor/${size}x${size}/apps/codehound-cody.png
done

# remove pip's entry point (we have our own wrapper)
rm -f %{buildroot}%{_bindir}/codehound  # pip may have written one; overwrite below
cat > %{buildroot}%{_bindir}/codehound << 'EOF'
#!/usr/bin/python3
from codehound.cli import main
import sys
sys.exit(main())
EOF
chmod 755 %{buildroot}%{_bindir}/codehound

%files
%{_bindir}/codehound
%{python3_sitelib}/codehound/
%{python3_sitelib}/codehound-*.dist-info/
%{_datadir}/applications/codehound.desktop
%{_datadir}/icons/hicolor/*/apps/codehound-cody.png

%post
# refresh desktop database and icon cache
/usr/bin/update-desktop-database %{_datadir}/applications &> /dev/null || :
/usr/bin/gtk-update-icon-cache %{_datadir}/icons/hicolor &> /dev/null || :

%postun
/usr/bin/update-desktop-database %{_datadir}/applications &> /dev/null || :
/usr/bin/gtk-update-icon-cache %{_datadir}/icons/hicolor &> /dev/null || :

%changelog
* Sat Oct 31 2026 zacpr <zac@ashurtech.net> - 0.5.1.4-1
- Config: launcher names are now quoted when the config is written. The
  writer emitted names as bare TOML keys, so a launcher whose name
  contains a space (e.g. 'Claude Code') produced an invalid config.toml
  and the app crashed on the next startup with TOMLDecodeError.

* Mon Oct 12 2026 zacpr <zac@ashurtech.net> - 0.5.1.3-1
- Updates: fixed 'codehound updates --install-all' crashing with
  AttributeError ('Namespace' has no attribute 'exe') — --exe was only
  defined on the 'update' subparser, so every install-all run died,
  including the dashboard's Install Updates button.

* Mon Oct 12 2026 zacpr <zac@ashurtech.net> - 0.5.1.2-1
- Updates: codeHOUND's own row now lights up with the release badge. The
  self-update row was written with an empty path, and the dashboard keys
  updates by project path, so our clone could never show it; the row now
  carries the path of the catalogued clone.
- Self row stays out of the bulk 'Install Updates' count and never opens
  'Update Settings…'; its context-menu 'Install Update' runs the
  self-update flow (quits and restarts on the new build) instead of the
  per-app installer, which refuses our own repo.

* Mon Oct 12 2026 zacpr <zac@ashurtech.net> - 0.5.1.1-1
- Updates: repos no longer on the watch list (un-catalogued, forked,
  transferred, or filtered out by own-only) are pruned from the
  updates table on the next check, so stale non-owned repos stop
  showing as available updates.
- Context menu: 'Update Settings…' now appears for any project with a
  GitHub remote, not only those with an update pending; settings saved
  before the first check seed the watch row.

* Mon Oct 12 2026 zacpr <zac@ashurtech.net> - 0.5.1.0-1
- Dashboard: update visibility and one-click installs. New 'Release
  Updated' column with glowing highlight for apps with a newer
  release; Updates filter button; sidebar Install Updates button
  (confirm, then install all); per-row Install Update and Update
  Settings (custom install path, auto/prompt/never policy); clearer
  tray notification; version shown in status bar and tray menu.
- Updates watching now defaults to repos you own (forks excluded);
  set [updates] own_only = false to watch every catalogued repo.
- New [updates] install_dir default path; per-app path overrides.
- CLI: 'codehound updates --install-all'.
- Fix: saving Settings no longer resets [updates] config; 'codehound
  update REPO' no longer crashes on the staged-activation path.

* Sun Oct 11 2026 zacpr <zac@ashurtech.net> - 0.5.0.3-1
- Fix: 'codehound update' could not download release assets from
  private GitHub repositories (HTTP 404). Assets are now fetched
  through the authenticated releases API with the same credentials
  used for release listing.

* Sun Oct 11 2026 zacpr <zac@ashurtech.net> - 0.5.0.2-1
- Fix: an RPM upgrade left the per-user systemd units behind, so the
  scan timer kept running the old command line (no 'updates --check').
  Units are stamped with a version marker; the first scan or app start
  after an upgrade rewrites them and reloads systemd.

* Sun Oct 11 2026 zacpr <zac@ashurtech.net> - 0.5.0-1
- Update manager: watches GitHub releases for every project in the
  catalogue and notifies when updates land (tray + CLI + timer)
- codehound updates / update: check, download, verify (published
  SHA256 when available) and install release assets into a private
  catalogue-managed installs/ tree, with activation, rollback and
  adoption of existing launchers
- Auto-update policy: stage-then-confirm — updates are downloaded and
  verified in the background, never swapped under a running app
- System installs: codehound update REPO --system installs .rpm/.deb
  release assets via pkexec (dnf/yum/apt/dpkg/rpm)
- Self-update: codehound self-update and a tray "Update codeHOUND…"
  entry compare the newest release against the installed version,
  install via pkexec, and quit-and-relaunch the tray app
- safepath: single chokepoint for every recursive delete, with a
  per-OS blacklist and containment checks; managed-install paths are
  validated against tampered catalogue rows

* Sun Oct 11 2026 zacpr <zac@ashurtech.net> - 0.4.0-1
- codehound detect: finds installed editors, AI agent CLIs (Claude Code,
  Codex, Gemini, Aider, Copilot, Cody, Kimi) wrapped in a terminal, and
  running OpenAI-compatible local model servers (LM Studio, OpenWebUI, ...)
- Settings: "Detect installed tools..." in Launchers and
  "Detect local AI services..." in the AI tab, both via a tick-list dialog

* Sun Oct 11 2026 zacpr <zac@ashurtech.net> - 0.3.0-1
- New Project: one button to create a brand-new project (git init, optional
  AI scaffold), clone an existing repo (URL, local path, or the catalogue),
  or add a worktree for a branch of a project already on disk
- Matching CLI: codehound new / clone / worktree
- Right-click a project to jump straight to the worktree tab
- Log per-directory progress while the LLM classifies ambiguous directories

* Sat Sep 26 2026 zacpr <zac@ashurtech.net> - 0.2.0-10
- Give Cody a distinct theme icon name so desktop shells cannot reuse the old icon cache

* Sat Sep 26 2026 zacpr <zac@ashurtech.net> - 0.2.0-9
- Branded dashboard with featured projects, pins, AI catch-up and project/branch labels
- Replace the orange icon with Cody H. Ound in the app, taskbar, menu and tray
- Include desktop icons from 16 to 256 pixels

* Sun Sep 06 2026 zacpr <zac@ashurtech.net> - 0.2.0-5
- Fix custom OpenAI-compatible endpoints (LM Studio etc.): a base URL given
  as bare host[:port] now targets the API root at /v1, so requests go to
  /v1/chat/completions instead of the rejected /chat/completions.

* Sun Sep 06 2026 zacpr <zac@ashurtech.net> - 0.2.0-4
- Fix system tray icon not appearing: the StatusNotifier protocol sends the
  icon by theme name, but the app built its QIcon from a bare SVG path —
  the panel received an empty IconName and drew nothing. Now uses
  QIcon.fromTheme("codehound", file-fallback).

* Sun Sep 06 2026 zacpr <zac@ashurtech.net> - 0.2.0-3
- Fix settings/janitor dialogs leaking ~0.7 MB each time they were opened
  (Qt object cycles kept the whole dialog alive; now WA_DeleteOnClose)
- Ask AI: when no provider is configured, clicking advises the user and
  offers to open Settings → AI; tray/context entries are disabled with a
  hint until then

* Sun Sep 06 2026 zacpr <zac@ashurtech.net> - 0.2.0-2
- Desktop entry: StartupNotify + StartupWMClass so menu launches give
  feedback and the window groups with its hound icon
- install-autostart: refresh desktop/icon caches so the menu icon
  appears straight away; keep autostart entry quiet at login

* Sun Sep 06 2026 zacpr <zac@ashurtech.net> - 0.2.0-1
- Settings screen, account mirroring, Ask AI, janitor, UX polish
- Initial RPM packaging
