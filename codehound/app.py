"""codeHOUND tray app and dashboard window."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
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
from .settings import SettingsDialog

ICON_PATH = Path(__file__).parent / "assets" / "hound.svg"


class AskDialog(QDialog):
    """Runs `codehound ask …` in the background and streams the reply."""

    def __init__(self, parent, cli_args: list[str], title: str, note: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(680, 480)
        layout = QVBoxLayout(self)
        if note:
            label = QLabel(note)
            label.setStyleSheet("color: palette(mid); font-style: italic;")
            layout.addWidget(label)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlainText("asking the model…\n")
        layout.addWidget(self.output)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy_btn = buttons.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
        copy_btn.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self.output.toPlainText())
        )
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._proc.readyReadStandardOutput.connect(self._append)
        self._proc.finished.connect(self._finished)
        self._proc.start(sys.executable, ["-m", "codehound.cli", *cli_args])

    def _append(self):
        text = self._proc.readAllStandardOutput().data().decode(errors="replace")
        if self.output.toPlainText() == "asking the model…\n":
            self.output.clear()
        self.output.appendPlainText(text.rstrip("\n"))
        self.output.verticalScrollBar().setValue(self.output.verticalScrollBar().maximum())

    def _finished(self, code, _status):
        if self.output.toPlainText() == "asking the model…\n":
            self.output.clear()
        if code != 0:
            self.output.appendPlainText(f"\n(ask exited with code {code} — check Settings → AI)")


class Dashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.setWindowTitle("codeHOUND")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1250, 650)
        self._scan_proc: QProcess | None = None
        self._sync_proc: QProcess | None = None
        self._digest_proc: QProcess | None = None

        central = QWidget()
        layout = QVBoxLayout(central)

        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name, path, description, language, branch…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        top.addWidget(self.search, 1)
        self.scan_btn = QPushButton("Scan Now")
        top.addWidget(self.scan_btn)
        self.sync_btn = QPushButton("Sync Accounts")
        top.addWidget(self.sync_btn)
        self.ask_btn = QPushButton("Ask AI…")
        top.addWidget(self.ask_btn)
        reload_btn = QPushButton("Reload")
        top.addWidget(reload_btn)
        settings_btn = QPushButton("Settings")
        top.addWidget(settings_btn)
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
        hdr.setSectionsMovable(True)
        hdr.sectionMoved.connect(self._save_column_order)
        layout.addWidget(self.table)
        self._restore_column_order()

        self.setCentralWidget(central)
        self.count_label = QLabel()
        self.statusBar().addPermanentWidget(self.count_label)

        self.search.textChanged.connect(self.proxy.set_needle)
        self.search.textChanged.connect(self._update_count)
        self.scan_btn.clicked.connect(self.start_scan)
        self.sync_btn.clicked.connect(self.start_sync)
        self.ask_btn.clicked.connect(self.ask_ai)
        reload_btn.clicked.connect(self.reload)
        settings_btn.clicked.connect(self.open_settings)
        self.table.doubleClicked.connect(self._open_default)
        self.table.customContextMenuRequested.connect(self._context_menu)

        find = QAction(self)
        find.setShortcut(QKeySequence.StandardKey.Find)
        find.triggered.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        self.addAction(find)

        self.reload()
        for col, width in ((0, 220), (1, 70), (2, 90), (3, 130), (4, 130), (5, 130), (6, 170), (7, 320), (9, 80)):
            self.table.setColumnWidth(col, width)

    # ----- columns -----
    def _save_column_order(self, *_args):
        hdr = self.table.horizontalHeader()
        order = [hdr.logicalIndex(v) for v in range(hdr.count())]
        conn = db.connect()
        try:
            db.kv_set(conn, "column_order", ",".join(map(str, order)))
            conn.commit()
        finally:
            conn.close()

    def _restore_column_order(self):
        conn = db.connect()
        try:
            raw = db.kv_get(conn, "column_order")
        finally:
            conn.close()
        if not raw:
            return
        try:
            order = [int(x) for x in raw.split(",")]
        except ValueError:
            return
        hdr = self.table.horizontalHeader()
        if len(order) != hdr.count() or sorted(order) != list(range(hdr.count())):
            return
        # order[visual] = logical; move each logical section to its visual slot
        for visual, logical in enumerate(order):
            current_visual = hdr.visualIndex(logical)
            if current_visual != visual:
                hdr.moveSection(current_visual, visual)

    # ----- data -----
    def reload(self):
        self.cfg = load_config()
        self.model.refresh()
        self._update_count()
        conn = db.connect()
        last = db.last_scan(conn)
        digest_at = db.kv_get(conn, "ai_digest_at")
        conn.close()
        self._digest_at = digest_at
        self._refresh_ask_button()
        if last:
            self.statusBar().showMessage(f"last scan: {last['finished']} ({last['projects_found']} projects)")
        else:
            self.statusBar().showMessage(f"no scans yet — hit Scan Now  ·  config: {CONFIG_PATH}")

    def _refresh_ask_button(self):
        from .ai import is_configured
        if not is_configured(self.cfg):
            self.ask_btn.setToolTip(
                "Ask across the catalogue — needs an AI provider (Settings → AI)"
            )
            return
        if self.cfg.ai_catalogue_summary:
            from .ask import digest_stale
            at = self._digest_at[5:16].replace("T", " ") if self._digest_at else "not generated yet"
            state = " — stale, projects changed since" if digest_stale(self._digest_at) else ""
            self.ask_btn.setToolTip(
                f"Ask across the catalogue (model-written summary, last updated {at}{state})"
            )
        else:
            self.ask_btn.setToolTip("Ask across the catalogue")

    def _update_count(self):
        self.count_label.setText(f"{self.proxy.rowCount()}/{self.model.rowCount()} projects")

    # ----- background CLI tasks (scan / sync) -----
    def _start_cli(self, attr: str, args: list[str], btn: QPushButton, busy: str, idle: str):
        if getattr(self, attr):
            return
        btn.setEnabled(False)
        btn.setText(busy)
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(
            lambda: self._task_output(proc.readAllStandardOutput().data().decode(errors="replace"))
        )
        proc.finished.connect(lambda code, _s: self._task_finished(attr, code, btn, idle, args[0]))
        proc.start(sys.executable, ["-m", "codehound.cli", *args])
        setattr(self, attr, proc)

    def start_scan(self):
        self._start_cli("_scan_proc", ["scan"], self.scan_btn, "Scanning…", "Scan Now")

    def start_sync(self):
        self._start_cli("_sync_proc", ["sync"], self.sync_btn, "Syncing…", "Sync Accounts")

    def refresh_digest(self):
        if not self.cfg.ai_catalogue_summary:
            self.statusBar().showMessage(
                "enable catalogue summaries in Settings → AI first")
            return
        self._start_cli("_digest_proc", ["ask", "--refresh-digest"],
                        self.ask_btn, "Summarising…", "Ask AI…")

    def _task_output(self, text: str):
        line = text.strip().splitlines()
        if line:
            self.statusBar().showMessage(line[-1][:200])

    def _task_finished(self, attr: str, code: int, btn: QPushButton, idle: str, what: str):
        setattr(self, attr, None)
        btn.setEnabled(True)
        btn.setText(idle)
        self.reload()
        if code != 0:
            self.statusBar().showMessage(f"{what} exited with code {code} — see journal/config")

    # ----- actions -----
    def open_settings(self):
        dlg = SettingsDialog(self.cfg, self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        if dlg.exec() == SettingsDialog.DialogCode.Accepted:
            self.reload()
            self.statusBar().showMessage(f"settings saved to {CONFIG_PATH}")

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
        from .ai import is_configured
        ask_menu = menu.addMenu("Ask AI")
        ask_menu.setEnabled(is_configured(self.cfg))
        if not is_configured(self.cfg):
            ask_menu.setToolTip("Configure an AI provider in Settings → AI first")
        ask_menu.addAction("Summarise this project").triggered.connect(
            lambda: self._ask_about(path, "Summarise this project: what it is, the tech stack, and its current state.")
        )
        ask_menu.addAction("What's it up to?").triggered.connect(
            lambda: self._ask_about(path, "Based on the recent commits and working tree, what is actively being worked on in this project?")
        )
        ask_menu.addAction("Custom question…").triggered.connect(lambda: self._ask_custom(path))
        menu.addSeparator()
        menu.addAction("Open in file manager").triggered.connect(
            lambda: subprocess.Popen(["xdg-open", path])
        )
        menu.addAction("Adopt into repos…").triggered.connect(lambda: self._adopt(path))
        menu.addAction("Copy path").triggered.connect(
            lambda: QGuiApplication.clipboard().setText(path)
        )
        menu.exec(self.table.viewport().mapToGlobal(pos))

    # ----- Ask AI -----
    def _show_answer(self, cli_args: list[str], title: str, note: str = ""):
        dlg = AskDialog(self, cli_args, title, note)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.show()

    def _ask_about(self, path: str, question: str):
        self._show_answer(["ask", "--path", path, question], f"Ask AI — {Path(path).name}")

    def _ask_custom(self, path: str):
        q, ok = QInputDialog.getMultiLineText(self, "Ask AI", f"Ask about {path}:")
        if ok and q.strip():
            self._ask_about(path, q.strip())

    def ask_ai(self):
        from .ai import is_configured
        if not is_configured(self.cfg):
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Icon.Information)
            box.setWindowTitle("Ask AI — no provider configured")
            box.setText(
                "Ask AI needs a model provider before it can answer questions.\n\n"
                "Open Settings → AI to pick a provider (Ollama runs locally;\n"
                "OpenAI, Gemini and Kimi need an API key)."
            )
            box.addButton("Open Settings", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Close)
            if box.exec() == 0:  # Open Settings
                self.open_settings()
            return
        menu = QMenu(self)
        menu.addAction("Ask a question…").triggered.connect(self._ask_catalogue_custom)
        if self.cfg.ai_catalogue_summary:
            from .ask import digest_stale
            menu.addSeparator()
            menu.addAction("Regenerate catalogue summary").triggered.connect(self.refresh_digest)
            at = self._digest_at[5:16].replace("T", " ") if self._digest_at else "never"
            state = " — STALE (projects changed since)" if digest_stale(self._digest_at) else ""
            info = menu.addAction(f"Summary last updated: {at}{state}")
            info.setEnabled(False)
        menu.exec(self.ask_btn.mapToGlobal(self.ask_btn.rect().bottomLeft()))

    def _ask_catalogue_custom(self):
        q, ok = QInputDialog.getMultiLineText(
            self, "Ask AI", "Ask across your whole project catalogue:"
        )
        if ok and q.strip():
            note = ""
            if self.cfg.ai_catalogue_summary:
                from .ask import digest_stale
                at = self._digest_at[5:16].replace("T", " ") if self._digest_at else "never"
                note = f"Answering from the model-written catalogue summary (updated {at})."
                if digest_stale(self._digest_at):
                    note += " ⚠ It may be stale — projects changed since; use Ask AI ▸ Regenerate."
            self._show_answer(["ask", q.strip()], "Ask AI — all projects", note)

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
    from .ai import is_configured
    ai_ready = is_configured(win.cfg)
    menu.addAction("Open Dashboard").triggered.connect(lambda: (win.show(), win.raise_(), win.activateWindow()))
    menu.addAction("Scan Now").triggered.connect(win.start_scan)
    menu.addAction("Sync Accounts").triggered.connect(win.start_sync)
    ask_act = menu.addAction("Ask AI…")
    ask_act.setEnabled(ai_ready)
    if ai_ready:
        ask_act.triggered.connect(lambda: (win.show(), win.raise_(), win._ask_catalogue_custom()))
    else:
        ask_act.setToolTip("Configure an AI provider in Settings → AI first")
    if ai_ready and win.cfg.ai_catalogue_summary:
        menu.addAction("Refresh AI summary").triggered.connect(win.refresh_digest)
    menu.addAction("Settings…").triggered.connect(win.open_settings)
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
