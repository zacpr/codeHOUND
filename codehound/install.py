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

ICON_PATH = Path(__file__).parent / "assets" / "hound.svg"
DESKTOP_PATH = Path(__file__).parent / "assets" / "codehound.desktop"

DESKTOP = """\
[Desktop Entry]
Type=Application
Name=codeHOUND
Comment=Repo hunter and launcher dashboard
Exec={exe} app
Icon=codehound
Terminal=false
Categories=Development;Utility;
X-KDE-StartupNotify=false
"""


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
    content = DESKTOP.format(exe=_exe())
    for d in (APPS_DIR, AUTOSTART_DIR):
        d.mkdir(parents=True, exist_ok=True)
        (d / "codehound.desktop").write_text(content)
    # install icon for the desktop file
    icons_dir = Path.home() / ".local/share/icons/hicolor/scalable/apps"
    icons_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(ICON_PATH, icons_dir / "codehound.svg")
    print(f"desktop entries installed: {APPS_DIR} and {AUTOSTART_DIR}")
    print(f"icon installed: {icons_dir / 'codehound.svg'}")
    return 0
