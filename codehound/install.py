"""Install systemd user timer and desktop/autostart entries."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SYSTEMD_DIR = Path.home() / ".config/systemd/user"
APPS_DIR = Path.home() / ".local/share/applications"
AUTOSTART_DIR = Path.home() / ".config/autostart"

# Bump whenever the SERVICE/TIMER templates change, so an upgraded codeHOUND
# notices a per-user unit written by an older version and rewrites it. Units
# written before markers existed read back as version 0.
UNIT_VERSION = 2
_UNIT_MARK = "# codehound-unit-version:"

SERVICE = """\
{mark} {unit_version}
[Unit]
Description=codeHOUND project scan

[Service]
Type=oneshot
ExecStart=-{exe} sync
ExecStart={exe} scan
ExecStart=-{exe} updates --check
Nice=19
IOSchedulingClass=idle
"""

TIMER = """\
{mark} {unit_version}
[Unit]
Description=codeHOUND periodic project scan

[Timer]
OnBootSec=5min
OnUnitActiveSec={every}
Persistent=true

[Install]
WantedBy=timers.target
"""

ICONS_PATH = Path(__file__).parent / "assets" / "icons"
DESKTOP_PATH = Path(__file__).parent / "assets" / "codehound.desktop"

DESKTOP = """\
[Desktop Entry]
Type=Application
Name=codeHOUND
Comment=Repo hunter and launcher dashboard
Exec={exe} app
Icon=codehound-cody
Terminal=false
Categories=Development;Utility;
StartupNotify=true
StartupWMClass=codehound
"""

# autostart copy: same entry, but no launch feedback at login
AUTOSTART = DESKTOP + "X-KDE-StartupNotify=false\n"


def _exe() -> str:
    """Absolute path to the codehound entry point (venv-safe)."""
    exe = Path(sys.argv[0]).resolve()
    if exe.name == "codehound":
        return str(exe)
    found = shutil.which("codehound")
    if found:
        return found
    return f"{sys.executable} -m codehound.cli"


def _unit_version(path: Path) -> int:
    """The unit-version marker written into an installed unit (0 if absent)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return -1  # missing or unreadable — needs (re)writing
    for line in text.splitlines():
        if line.startswith(_UNIT_MARK):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return 0
    return 0


def units_current() -> bool:
    """True when the installed systemd units match this codeHOUND version.

    An RPM upgrade replaces the code but not the per-user unit files, so a
    unit written by an older release keeps running the old command line (for
    example one without 'updates --check'). Only the marker is compared: a
    source checkout must never rewrite units that belong to the installed
    package, so a matching marker is taken as good news.
    """
    for name in ("codehound-scan.service", "codehound-scan.timer"):
        if _unit_version(SYSTEMD_DIR / name) < UNIT_VERSION:
            return False
    return True


def ensure_units(progress=print) -> bool:
    """Rewrite + reload the systemd units when they are stale. Never raises."""
    try:
        if units_current():
            return False
        progress("systemd units are from an older codeHOUND — refreshing…")
        if install_timer(progress=progress) != 0:
            progress("could not refresh the scan timer; run "
                     "'codehound install-timer' by hand")
            return False
        progress("scan timer refreshed")
        return True
    except Exception as exc:  # self-heal must never break a scan
        progress(f"unit refresh skipped: {exc}")
        return False


def install_timer(every: str = "6h", progress=print) -> int:
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    args = {"exe": _exe(), "every": every,
            "mark": _UNIT_MARK, "unit_version": UNIT_VERSION}
    (SYSTEMD_DIR / "codehound-scan.service").write_text(SERVICE.format(**args))
    (SYSTEMD_DIR / "codehound-scan.timer").write_text(TIMER.format(**args))
    for cmd in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "codehound-scan.timer"],
    ):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            progress(f"{' '.join(cmd)} failed: {r.stderr.strip()}")
            return 1
    progress(f"timer installed: scans every {every} (+5min after boot)")
    progress("check with: systemctl --user list-timers codehound-scan.timer")
    return 0


def install_autostart() -> int:
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    (APPS_DIR / "codehound.desktop").write_text(DESKTOP.format(exe=_exe()))
    AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    (AUTOSTART_DIR / "codehound.desktop").write_text(AUTOSTART.format(exe=_exe()))
    # install icon for the desktop file
    icons_dir = Path.home() / ".local/share/icons/hicolor"
    install_icons(icons_dir)
    _refresh_desktop_caches()
    print(f"desktop entries installed: {APPS_DIR} and {AUTOSTART_DIR}")
    print(f"Cody H. Ound icons installed: {icons_dir}")
    return 0


def install_icons(icons_dir: Path) -> None:
    """Install each raster size and retire the old orange scalable icon."""
    sources = sorted(ICONS_PATH.glob("*/apps/codehound.png"))
    if not sources:
        raise FileNotFoundError(f"No codeHOUND icons found in {ICONS_PATH}")
    for source in sources:
        target = (icons_dir / source.relative_to(ICONS_PATH)).with_name("codehound-cody.png")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source, target)
    (icons_dir / "scalable/apps/codehound.svg").unlink(missing_ok=True)


def _refresh_desktop_caches() -> None:
    """Best-effort refresh so the menu entry and icon show up immediately."""
    for cmd in (
        ["update-desktop-database", str(APPS_DIR)],
        ["gtk-update-icon-cache", "-q", str(Path.home() / ".local/share/icons/hicolor")],
        ["kbuildsycoca6", "--noincremental"],
    ):
        if shutil.which(cmd[0]):
            subprocess.run(cmd, capture_output=True)
