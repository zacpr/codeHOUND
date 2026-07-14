"""codeHOUND tray app and dashboard window."""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from . import adopt as adopt_mod
from . import db, launchers
from .config import CONFIG_PATH, load_config
from .model import PATH_ROLE, ProjectsModel, ProjectsProxy

ICON_PATH = Path(__file__).parent / "assets" / "hound.svg"


def _scan_command() -> list[str]:
    return [sys.executable, "-m", "codehound.cli", "scan"]


class Dashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.setWindowTitle("codeHOUND")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1250, 650)
        self._scan_proc: QProcess | None = None

        central = QWidget()
        layout = QVBoxLayout(central)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name, path, description, language, branch…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        top.addWidget(self.search, 1)
        self.scan_btn = QPushButton("Scan Now")
        top.addWidget(self.scan_btn)
        reload_btn = QPushButton("Reload")
        top.addWidget(reload_btn)
        layout.addLayout(top)

        self.model = ProjectsModel(self)
        self.proxy = ProjectsProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(4, Qt.SortOrder.DescendingOrder)  # Modified
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        layout.addWidget(self.table)

        self.setCentralWidget(central)
        self.count_label = QLabel()
        self.statusBar().addPermanentWidget(self.count_label)

        self.search.textChanged.connect(self.proxy.set_needle)
        self.search.textChanged.connect(self._update_count)
        self.scan_btn.clicked.connect(self.start_scan)
        reload_btn.clicked.connect(self.reload)
        self.table.doubleClicked.connect(self._open_default)
        self.table.customContextMenuRequested.connect(self._context_menu)

        find = QAction(self)
        find.setShortcut(QKeySequence.StandardKey.Find)
        find.triggered.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        self.addAction(find)

        self.reload()
        for col, width in ((0, 220), (1, 70), (2, 90), (3, 130), (4, 130), (5, 130), (6, 170), (7, 320)):
            self.table.setColumnWidth(col, width)

    # ----- data -----
    def reload(self):
        self.cfg = load_config()
        self.model.refresh()
        self._update_count()
        conn = db.connect()
        last = db.last_scan(conn)
        conn.close()
        if last:
            self.statusBar().showMessage(f"last scan: {last['finished']} ({last['projects_found']} projects)")
        else:
            self.statusBar().showMessage(f"no scans yet — hit Scan Now  ·  config: {CONFIG_PATH}")

    def _update_count(self):
        self.count_label.setText(f"{self.proxy.rowCount()}/{self.model.rowCount()} projects")

    # ----- scanning -----
    def start_scan(self):
        if self._scan_proc:
            return
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("Scanning…")
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(
            lambda: self._scan_output(proc.readAllStandardOutput().data().decode(errors="replace"))
        )
        proc.finished.connect(self._scan_finished)
        proc.start(_scan_command()[0], _scan_command()[1:])
        self._scan_proc = proc

    def _scan_output(self, text: str):
        line = text.strip().splitlines()
        if line:
            self.statusBar().showMessage(line[-1][:200])

    def _scan_finished(self, code, _status):
        self._scan_proc = None
        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("Scan Now")
        self.reload()
        if code != 0:
            self.statusBar().showMessage(f"scan exited with code {code} — see journal/config")

    # ----- actions -----
    def _selected_path(self, index=None) -> str | None:
        idx = index if index is not None and index.isValid() else self.table.currentIndex()
        if not idx.isValid():
            return None
        return self.proxy.data(idx, PATH_ROLE)

    def _open_default(self, index):
        path = self._selected_path(index)
        names = launchers.launcher_names(self.cfg)
        if path and names:
            launchers.launch(self.cfg, names[0], path)

    def _context_menu(self, pos):
        index = self.table.indexAt(pos)
        path = self._selected_path(index)
        if not path:
            return
        menu = QMenu(self)
        for name in launchers.launcher_names(self.cfg):
            act = menu.addAction(f"Open in {name}")
            act.triggered.connect(lambda _=False, n=name, p=path: launchers.launch(self.cfg, n, p))
        menu.addSeparator()
        menu.addAction("Adopt into repos…").triggered.connect(lambda: self._adopt(path))
        menu.addAction("Copy path").triggered.connect(
            lambda: QGuiApplication.clipboard().setText(path)
        )
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _adopt(self, path: str):
        suggested = adopt_mod.suggest_name(self.cfg, path)
        name, ok = QInputDialog.getText(
            self, "Adopt project",
            f"Copy\n{path}\ninto {self.cfg.adopt_dest} as:",
            text=suggested,
        )
        if not ok or not name.strip():
            return
        self.statusBar().showMessage(f"adopting {path}…")
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        out: list[str] = []
        proc.readyReadStandardOutput.connect(
            lambda: out.append(proc.readAllStandardOutput().data().decode(errors="replace"))
        )
        proc.finished.connect(lambda code, _s: self._adopt_finished(code, "".join(out)))
        proc.start(sys.executable, ["-m", "codehound.cli", "adopt", path, "--name", name.strip()])

    def _adopt_finished(self, code: int, output: str):
        self.reload()
        if code == 0:
            self.statusBar().showMessage(output.strip().splitlines()[-1] if output.strip() else "adopted")
        else:
            QMessageBox.warning(self, "Adopt failed", output[-1000:] or f"exit code {code}")

    # ----- window management -----
    def closeEvent(self, event):
        event.ignore()
        self.hide()


def run_app() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("codeHOUND")
    app.setDesktopFileName("codehound")
    app.setQuitOnLastWindowClosed(False)
    icon = QIcon(str(ICON_PATH))
    app.setWindowIcon(icon)

    win = Dashboard()

    tray = QSystemTrayIcon(icon)
    tray.setToolTip("codeHOUND — repo hunter")
    menu = QMenu()
    menu.addAction("Open Dashboard").triggered.connect(lambda: (win.show(), win.raise_(), win.activateWindow()))
    menu.addAction("Scan Now").triggered.connect(win.start_scan)
    menu.addSeparator()
    menu.addAction("Quit").triggered.connect(app.quit)
    tray.setContextMenu(menu)

    def on_activated(reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if win.isVisible():
                win.hide()
            else:
                win.show()
                win.raise_()
                win.activateWindow()

    tray.activated.connect(on_activated)
    tray.show()

    win.show()
    return app.exec()
