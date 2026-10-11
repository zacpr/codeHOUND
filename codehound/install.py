"""Install systemd user timer and desktop/autostart entries."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

SYSTEMD_DIR = Path.home() / ".config/systemd/user"
APPS_DIR = Path.home() / ".local/share/applications"
AUTOSTART_DIR = Path.home() / ".config/autostart"

SERVICE = """\
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


def install_timer(every: str = "6h") -> int:
    SYSTEMD_DIR.mkdir(parents=True, exist_ok=True)
    (SYSTEMD_DIR / "codehound-scan.service").write_text(SERVICE.format(exe=_exe()))
    (SYSTEMD_DIR / "codehound-scan.timer").write_text(TIMER.format(every=every))
    for cmd in (
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "codehound-scan.timer"],
    ):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"{' '.join(cmd)} failed: {r.stderr.strip()}", file=sys.stderr)
            return 1
    print(f"timer installed: scans every {every} (+5min after boot)")
    print("check with: systemctl --user list-timers codehound-scan.timer")
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
