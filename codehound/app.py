"""codeHOUND tray app and dashboard window."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QAction, QGuiApplication, QIcon, QKeySequence, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QFileDialog,
    QFormLayout,
    QFrame,
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
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTabWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from . import adopt as adopt_mod
from . import __version__, db, launchers
from .config import CONFIG_PATH, load_config
from .model import (
    COLUMNS,
    DEFAULT_HIDDEN_COLUMNS,
    PATH_ROLE,
    PROJECT_VIEWS,
    UPDATE_ROLE,
    ProjectsModel,
    ProjectsProxy,
    UpdateRowDelegate,
    project_label,
)
from .settings import SettingsDialog
from .theme import apply_theme

ICON_PATH = Path(__file__).parent / "assets" / "hound.png"
MASCOT_PATH = Path(__file__).parent / "assets" / "mascot.png"


def _label(text: str = "", *, muted: bool = False, eyebrow: bool = False) -> QLabel:
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setProperty("muted", muted)
    label.setProperty("eyebrow", eyebrow)
    return label


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


def _catalogue_projects(kind: str | None = None) -> list:
    """Live catalogue rows for the dialog pickers, newest first."""
    conn = db.connect()
    try:
        rows = [r for r in db.all_projects(conn) if not r["missing"]]
    finally:
        conn.close()
    if kind:
        rows = [r for r in rows if r["kind"] == kind]
    return rows


class NewProjectDialog(QDialog):
    """Create a new project, clone one, or add a worktree — hands off to the CLI."""

    def __init__(self, cfg, parent=None, default_tab: int = 0, project_path: str | None = None):
        super().__init__(parent)
        self.cfg = cfg
        self.setWindowTitle("New Project")
        self.resize(620, 460)
        layout = QVBoxLayout(self)

        from .ai import is_configured
        self._ai_ready = is_configured(cfg)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # --- New ---
        new_tab = QWidget()
        new_form = QFormLayout(new_tab)
        self.new_name = QLineEdit()
        self.new_name.setPlaceholderText("my-new-tool")
        new_form.addRow("Name:", self.new_name)
        self.new_desc = QPlainTextEdit()
        self.new_desc.setPlaceholderText("What is it? (goes in the README)")
        self.new_desc.setMaximumHeight(72)
        new_form.addRow("Description:", self.new_desc)
        self.new_scaffold = QCheckBox("Ask AI to scaffold starter files")
        if not self._ai_ready:
            self.new_scaffold.setEnabled(False)
            self.new_scaffold.setToolTip("Configure an AI provider in Settings → AI first")
        new_form.addRow(self.new_scaffold)
        self.new_dest = QLineEdit(cfg.adopt_dest)
        new_form.addRow("In folder:", self._dest_row(self.new_dest))
        self.tabs.addTab(new_tab, "New project")

        # --- Clone ---
        clone_tab = QWidget()
        clone_form = QFormLayout(clone_tab)
        self.clone_source = QLineEdit()
        self.clone_source.setPlaceholderText("https://… or git@host:owner/repo.git, or a local path")
        clone_form.addRow("Source:", self.clone_source)
        self.clone_pick = QComboBox()
        self.clone_pick.addItem("— pick a catalogue project to fill the source —", None)
        for row in _catalogue_projects():
            hint = row["remote_url"] or row["path"]
            self.clone_pick.addItem(f"{row['name']}  ({hint})", (row["path"], row["remote_url"]))
        self.clone_pick.currentIndexChanged.connect(self._clone_pick_changed)
        clone_form.addRow("Or from catalogue:", self.clone_pick)
        self.clone_name = QLineEdit()
        self.clone_name.setPlaceholderText("optional — defaults to the source name")
        clone_form.addRow("Rename to:", self.clone_name)
        self.clone_dest = QLineEdit(cfg.clone_dest)
        clone_form.addRow("Into folder:", self._dest_row(self.clone_dest))
        self.tabs.addTab(clone_tab, "Clone")

        # --- Worktree ---
        wt_tab = QWidget()
        wt_form = QFormLayout(wt_tab)
        self.wt_project = QComboBox()
        self.wt_project.addItem("— pick a project —", None)
        for row in _catalogue_projects("git"):
            self.wt_project.addItem(f"{row['name']}  ({row['path']})", row["path"])
        wt_form.addRow("Project:", self.wt_project)
        self.wt_branch = QLineEdit()
        self.wt_branch.setPlaceholderText("feature-x — new branch, or an existing one")
        self.wt_branch.textChanged.connect(self._wt_preview)
        wt_form.addRow("Branch:", self.wt_branch)
        self.wt_dest = _label("(pick a project and branch)", muted=True)
        wt_form.addRow("Worktree lands at:", self.wt_dest)
        self.tabs.addTab(wt_tab, "Add worktree")

        self.error = _label()
        self.error.setStyleSheet("color: #f38ba8;")
        self.error.setVisible(False)
        layout.addWidget(self.error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok_btn.setProperty("primary", True)
        buttons.accepted.connect(self._check_and_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.tabs.setCurrentIndex(default_tab)
        if project_path:
            idx = self.wt_project.findData(project_path)
            if idx >= 0:
                self.wt_project.setCurrentIndex(idx)
                self._wt_preview()

    def _dest_row(self, edit: QLineEdit) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(
            lambda: (p := QFileDialog.getExistingDirectory(self, "Choose folder", edit.text()))
            and edit.setText(p)
        )
        row.addWidget(browse)
        return row

    def _clone_pick_changed(self, _index):
        data = self.clone_pick.currentData()
        if data:
            _path, remote = data
            self.clone_source.setText(remote or _path)

    def _wt_preview(self, _text=""):
        src = self.wt_project.currentData()
        branch = self.wt_branch.text().strip()
        if src and branch:
            slug = re.sub(r"[^\w.-]+", "-", branch).strip("-") or "branch"
            self.wt_dest.setText(str(Path(src).parent / f"{Path(src).name}-{slug}"))
        else:
            self.wt_dest.setText("(pick a project and branch)")

    def _check_and_accept(self):
        tab = self.tabs.currentIndex()
        if tab == 0 and not self.new_name.text().strip():
            self.error.setText("Give the new project a name.")
        elif tab == 1 and not self.clone_source.text().strip():
            self.error.setText("Paste a git URL / local path, or pick a catalogue project.")
        elif tab == 2 and (not self.wt_project.currentData() or not self.wt_branch.text().strip()):
            self.error.setText("Pick a project and a branch name.")
        else:
            self.accept()
            return
        self.error.setVisible(True)

    def args(self) -> list[str]:
        """CLI argv for the selected tab (validated by _check_and_accept)."""
        tab = self.tabs.currentIndex()
        if tab == 0:
            out = ["new", self.new_name.text().strip()]
            desc = self.new_desc.toPlainText().strip()
            if desc:
                out += ["--description", desc]
            if self.new_scaffold.isChecked():
                out.append("--scaffold")
            if dest := self.new_dest.text().strip():
                out += ["--dest", dest]
            return out
        if tab == 1:
            out = ["clone", self.clone_source.text().strip()]
            if name := self.clone_name.text().strip():
                out += ["--name", name]
            if dest := self.clone_dest.text().strip():
                out += ["--dest", dest]
            return out
        return ["worktree", self.wt_project.currentData(), self.wt_branch.text().strip()]


class _UpdateSettingsDialog(QDialog):
    """Per-app update settings: custom install location + install policy."""

    def __init__(self, cfg, repo: str, name: str, path: str = "", parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.repo = repo
        self.name = name
        self.path = path
        self.setWindowTitle(f"Update Settings — {name or repo}")
        form = QFormLayout(self)
        conn = db.connect()
        try:
            row = db.get_update(conn, repo)
        finally:
            conn.close()
        form.addRow(QLabel(f"Watching {repo} for new GitHub releases."))

        path_row = QHBoxLayout()
        self.install_path = QLineEdit((row["install_path"] if row else "") or "")
        self.install_path.setPlaceholderText(
            "default — replace in place, else a managed dir with a ~/.local/bin link")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.install_path.clear)
        path_row.addWidget(self.install_path, 1)
        path_row.addWidget(browse)
        path_row.addWidget(clear)
        holder = QWidget()
        holder.setLayout(path_row)
        form.addRow("Install location:", holder)

        self.policy = QComboBox()
        self.policy.addItem("Follow the global setting", "default")
        self.policy.addItem("Install automatically", "auto")
        self.policy.addItem("Never install (check only)", "never")
        mode = {(1): "auto", (0): "never"}.get(row["auto"] if row else None, "default")
        self.policy.setCurrentIndex(self.policy.findData(mode))
        form.addRow("When an update appears:", self.policy)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Custom install directory",
                                                self.install_path.text())
        if path:
            self.install_path.setText(path)

    def _save(self):
        conn = db.connect()
        try:
            if db.get_update(conn, self.repo) is None:
                # Settings chosen before the first release check — seed the
                # watch row so the flags have somewhere to live.
                db.upsert_update(conn, {
                    "repo": self.repo, "path": self.path, "name": self.name,
                    "current_tag": "", "latest_tag": None, "published_at": None,
                    "asset_name": None, "asset_url": None, "available": 0,
                    "compatible": 0, "reason": "not checked yet",
                    "last_checked": None})
            db.set_update_install_path(conn, self.repo, self.install_path.text().strip())
            db.set_update_auto(conn, self.repo, self.policy.currentData())
            conn.commit()
        finally:
            conn.close()
        self.accept()


class Dashboard(QMainWindow):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.setWindowTitle("codeHOUND")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1440, 860)
        self.setMinimumSize(1060, 740)
        self._scan_proc: QProcess | None = None
        self._sync_proc: QProcess | None = None
        self._digest_proc: QProcess | None = None
        self._new_proc: QProcess | None = None
        self._updates_proc: QProcess | None = None
        self._staged_count = 0
        self.tray = None   # set by run_app() once the tray icon exists
        self._view = "all"
        self._pinned_paths = self._load_pins()
        self._featured: list[dict] = []
        self._featured_index = 0

        central = QWidget()
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(self._build_sidebar())

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 22, 24, 16)
        layout.setSpacing(16)
        shell.addWidget(content, 1)

        heading = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(4)
        titles.addWidget(_label("THE PROJECT DEN", eyebrow=True))
        self.page_title = _label("All projects")
        self.page_title.setObjectName("pageTitle")
        titles.addWidget(self.page_title)
        self.page_subtitle = _label(PROJECT_VIEWS["all"][1], muted=True)
        titles.addWidget(self.page_subtitle)
        heading.addLayout(titles, 1)
        self.ask_btn = QPushButton("Ask AI…")
        self.ask_btn.setProperty("accent", True)
        self.ask_btn.setToolTip("Ask across the catalogue")
        heading.addWidget(self.ask_btn)
        self.new_btn = QPushButton("New Project…")
        self.new_btn.setProperty("accent", True)
        self.new_btn.setToolTip("Create a new project, clone one, or add a worktree")
        heading.addWidget(self.new_btn)
        self.scan_btn = QPushButton("Scan Now")
        self.scan_btn.setProperty("primary", True)
        heading.addWidget(self.scan_btn)
        layout.addLayout(heading)

        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        self.metric_buttons = {}
        for view, title in (("all", "IN THE DEN"), ("git", "GIT REPOS"),
                            ("dirty", "WITH CHANGES"), ("duplicates", "DUPLICATE CLONES")):
            button = QPushButton()
            button.setProperty("metric", True)
            button.setMinimumHeight(78)
            metric_layout = QVBoxLayout(button)
            metric_layout.setContentsMargins(14, 8, 14, 8)
            metric_layout.setSpacing(1)
            value = _label()
            value.setProperty("metricValue", True)
            caption = _label(title, muted=True)
            for label in (value, caption):
                label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
                metric_layout.addWidget(label)
            button.setToolTip(f"Show {PROJECT_VIEWS[view][0].lower()}")
            button.clicked.connect(lambda _=False, v=view: self._set_view(v))
            self.metric_buttons[view] = (button, value, title)
            metrics.addWidget(button, 1)
        self.metrics_panel = QWidget()
        metrics.setContentsMargins(0, 0, 0, 0)
        self.metrics_panel.setLayout(metrics)
        layout.addWidget(self.metrics_panel)
        self.feature_panel = self._build_featured()
        layout.addWidget(self.feature_panel)

        tools = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setAccessibleName("Search projects")
        self.search.setPlaceholderText("Search projects, paths, languages…  Ctrl+F")
        self.search.setClearButtonEnabled(True)
        tools.addWidget(self.search, 1)
        self.updates_filter = QPushButton("Updates")
        self.updates_filter.setCheckable(True)
        self.updates_filter.setToolTip("Show only projects with a newer release available")
        tools.addWidget(self.updates_filter)
        reload_btn = QPushButton("Reload")
        reload_btn.setToolTip("Reload catalogue from disk (F5)")
        tools.addWidget(reload_btn)
        columns_btn = QPushButton("Columns")
        tools.addWidget(columns_btn)
        self.feature_toggle = QPushButton("Featured")
        self.feature_toggle.setCheckable(True)
        self.feature_toggle.setChecked(True)
        self.feature_toggle.toggled.connect(self.feature_panel.setVisible)
        tools.addWidget(self.feature_toggle)
        self.details_btn = QPushButton("Inspector")
        self.details_btn.setCheckable(True)
        self.details_btn.setChecked(True)
        tools.addWidget(self.details_btn)
        layout.addLayout(tools)

        self.model = ProjectsModel(self)
        self.proxy = ProjectsProxy(self)
        self.proxy.setSourceModel(self.model)
        self.table = QTableView()
        self.table.setObjectName("projects")
        self.table.setAccessibleName("Project catalogue")
        self.table.setModel(self.proxy)
        self.table.setItemDelegate(UpdateRowDelegate(self.table))
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(3, Qt.SortOrder.DescendingOrder)  # Modified
        self.table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(42)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        hdr.setSectionsMovable(True)
        hdr.sectionMoved.connect(self._save_column_order)
        self.results = QStackedWidget()
        self.results.addWidget(self.table)
        empty = QWidget()
        empty_layout = QVBoxLayout(empty)
        empty_layout.addStretch()
        self.empty_title = _label()
        self.empty_title.setObjectName("detailTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_help = _label(muted=True)
        self.empty_help.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_help)
        reset_btn = QPushButton("Show all projects")
        reset_btn.clicked.connect(self._reset_filters)
        empty_layout.addWidget(reset_btn, alignment=Qt.AlignmentFlag.AlignCenter)
        empty_layout.addStretch()
        self.results.addWidget(empty)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setHandleWidth(12)
        self.splitter.addWidget(self.results)
        self.inspector = self._build_inspector()
        self.splitter.addWidget(self.inspector)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, False)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setSizes([850, 280])
        layout.addWidget(self.splitter, 1)
        self._restore_column_order()
        columns_menu = QMenu(self)
        for col, (title, _) in enumerate(COLUMNS):
            action = columns_menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(col not in DEFAULT_HIDDEN_COLUMNS)
            self.table.setColumnHidden(col, not action.isChecked())
            action.toggled.connect(lambda checked, c=col: self.table.setColumnHidden(c, not checked))
        columns_btn.setMenu(columns_menu)

        self.setCentralWidget(central)
        self.count_label = QLabel()
        self.statusBar().addPermanentWidget(self.count_label)
        self.version_label = QLabel(f"v{__version__}")
        self.version_label.setProperty("muted", True)
        self.version_label.setToolTip(f"codeHOUND {__version__}")
        self.statusBar().addPermanentWidget(self.version_label)
        self.activity = QPlainTextEdit()
        self.activity.setReadOnly(True)
        self.activity.setMaximumBlockCount(500)
        self.activity.setPlaceholderText("Scan, sync and summary output will appear here.")
        self.activity_dock = QDockWidget("Activity", self)
        self.activity_dock.setWidget(self.activity)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.activity_dock)
        self.activity_dock.hide()
        self.activity_btn.clicked.connect(
            lambda: self.activity_dock.setVisible(not self.activity_dock.isVisible())
        )

        self.search.textChanged.connect(self._filter_changed)
        self.updates_filter.toggled.connect(self._filter_updates_only)
        self.scan_btn.clicked.connect(self.start_scan)
        self.sync_btn.clicked.connect(self.start_sync)
        self.updates_btn.clicked.connect(self.start_updates)
        self.install_updates_btn.clicked.connect(self.install_all_updates)
        self.ask_btn.clicked.connect(self.ask_ai)
        self.new_btn.clicked.connect(self.new_project)
        reload_btn.clicked.connect(self.reload)
        self.details_btn.toggled.connect(self.inspector.setVisible)
        self.table.doubleClicked.connect(self._open_default)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.selectionModel().selectionChanged.connect(self._update_inspector)

        find = QAction(self)
        find.setShortcut(QKeySequence.StandardKey.Find)
        find.triggered.connect(lambda: (self.search.setFocus(), self.search.selectAll()))
        self.addAction(find)
        reload_action = QAction(self)
        reload_action.setShortcut(QKeySequence("F5"))
        reload_action.triggered.connect(self.reload)
        self.addAction(reload_action)

        self.reload()
        for col, width in ((0, 210), (1, 115), (2, 280), (3, 155), (4, 95),
                           (5, 95), (6, 140), (9, 170)):
            self.table.setColumnWidth(col, width)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(202)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(16, 20, 16, 18)
        layout.setSpacing(8)
        mascot = QLabel()
        mascot.setAccessibleName("Cody H. Ound, the codeHOUND mascot")
        mascot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = QPixmap(str(MASCOT_PATH))
        if pixmap.isNull():
            raise RuntimeError(f"Cannot load mascot image: {MASCOT_PATH}")
        mascot.setPixmap(pixmap.scaled(160, 190, Qt.AspectRatioMode.KeepAspectRatio,
                                       Qt.TransformationMode.SmoothTransformation))
        layout.addWidget(mascot)
        tagline = _label("GOOD CODE. FOUND.", eyebrow=True)
        tagline.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(tagline)
        layout.addSpacing(20)
        layout.addWidget(_label("CATALOGUE", muted=True))
        self.nav_group = QButtonGroup(self)
        self.nav_buttons = {}
        for key, (title, subtitle) in PROJECT_VIEWS.items():
            btn = QPushButton(title)
            btn.setProperty("nav", True)
            btn.setCheckable(True)
            btn.setChecked(key == "all")
            btn.setToolTip(subtitle)
            btn.clicked.connect(lambda _=False, v=key: self._set_view(v))
            self.nav_group.addButton(btn)
            self.nav_buttons[key] = btn
            layout.addWidget(btn)
        layout.addStretch()
        self.sync_btn = QPushButton("Sync Accounts")
        layout.addWidget(self.sync_btn)
        self.updates_btn = QPushButton("Check Updates")
        self.updates_btn.setToolTip("Look for newer GitHub releases of watched projects")
        layout.addWidget(self.updates_btn)
        self.install_updates_btn = QPushButton("Install Updates")
        self.install_updates_btn.setProperty("accent", True)
        self.install_updates_btn.setEnabled(False)
        self.install_updates_btn.setToolTip("No updates available yet")
        layout.addWidget(self.install_updates_btn)
        self.activity_btn = QPushButton("Activity")
        layout.addWidget(self.activity_btn)
        settings = QPushButton("Settings")
        settings.clicked.connect(self.open_settings)
        layout.addWidget(settings)
        return sidebar

    def _build_inspector(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("inspector")
        panel.setMinimumWidth(245)
        panel.setMaximumWidth(390)
        panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(18, 20, 18, 20)
        layout.setSpacing(12)
        layout.addWidget(_label("ON THE SCENT", eyebrow=True))
        self.detail_title = _label("Pick a project")
        self.detail_title.setObjectName("detailTitle")
        layout.addWidget(self.detail_title)
        self.detail_meta = _label(muted=True)
        layout.addWidget(self.detail_meta)
        self.detail_description = _label()
        self.detail_description.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.detail_description)
        self.detail_path = _label(muted=True)
        self.detail_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(_label("LOCATION", eyebrow=True))
        layout.addWidget(self.detail_path)
        layout.addWidget(_label("LATEST COMMIT", eyebrow=True))
        self.detail_commit = _label(muted=True)
        layout.addWidget(self.detail_commit)
        layout.addStretch()
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)
        actions = QWidget()
        action_layout = QVBoxLayout(actions)
        action_layout.setContentsMargins(14, 4, 14, 14)
        action_layout.setSpacing(8)
        launch_row = QHBoxLayout()
        self.launcher_combo = QComboBox()
        self.launcher_combo.setAccessibleName("Project launcher")
        self.launcher_combo.setMinimumContentsLength(6)
        self.launcher_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        launch_row.addWidget(self.launcher_combo, 1)
        self.open_btn = QPushButton("Open")
        self.open_btn.setProperty("primary", True)
        self.open_btn.clicked.connect(self._open_selected)
        launch_row.addWidget(self.open_btn)
        action_layout.addLayout(launch_row)
        self.project_ask_btn = QPushButton("Ask about this project…")
        self.project_ask_btn.setProperty("accent", True)
        self.project_ask_btn.clicked.connect(self._ask_selected)
        action_layout.addWidget(self.project_ask_btn)
        utility_row = QHBoxLayout()
        self.copy_path_btn = QPushButton("Copy path")
        self.copy_path_btn.clicked.connect(self._copy_selected_path)
        utility_row.addWidget(self.copy_path_btn)
        self.pin_btn = QPushButton("Pin")
        self.pin_btn.clicked.connect(self._pin_selected)
        self.pin_btn.setToolTip("Keep this project in the featured carousel, beyond the five most recent")
        utility_row.addWidget(self.pin_btn)
        action_layout.addLayout(utility_row)
        outer.addWidget(actions)
        return panel

    def _load_pins(self) -> set[str]:
        conn = db.connect()
        try:
            raw = db.kv_get(conn, "featured_pins")
        finally:
            conn.close()
        if raw is None:
            return set()
        try:
            paths = json.loads(raw)
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise ValueError("Expected a list of project paths")
        except (ValueError, TypeError) as error:
            QMessageBox.warning(self, "Featured projects", f"Could not load pinned projects: {error}")
            return set()
        return set(paths)

    def _build_featured(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("featured")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(10)
        header = QHBoxLayout()
        feature_heading = _label("CONTINUE THE HUNT", eyebrow=True)
        feature_heading.setWordWrap(False)
        header.addWidget(feature_heading)
        header.addWidget(_label("5 most recent + your pins · across the catalogue", muted=True), 1)
        self.feature_position = _label()
        header.addWidget(self.feature_position)
        self.feature_prev = QPushButton("‹")
        self.feature_prev.setAccessibleName("Previous featured project")
        self.feature_next = QPushButton("›")
        self.feature_next.setAccessibleName("Next featured project")
        for button, step in ((self.feature_prev, -1), (self.feature_next, 1)):
            button.setFixedWidth(36)
            button.clicked.connect(lambda _=False, s=step: self._step_featured(s))
            header.addWidget(button)
        layout.addLayout(header)
        story = QHBoxLayout()
        story.setSpacing(24)
        intro = QVBoxLayout()
        self.feature_name = _label()
        self.feature_name.setObjectName("featureName")
        intro.addWidget(self.feature_name)
        self.feature_meta = _label(muted=True)
        intro.addWidget(self.feature_meta)
        intro.addStretch()
        story.addLayout(intro, 3)
        summary = QVBoxLayout()
        summary.addWidget(_label("AI SUMMARY · SAVED", eyebrow=True))
        self.feature_summary = _label()
        self.feature_summary.setMinimumHeight(self.feature_summary.fontMetrics().lineSpacing() * 5)
        self.feature_summary.setAlignment(Qt.AlignmentFlag.AlignTop)
        summary.addWidget(self.feature_summary)
        summary.addStretch()
        story.addLayout(summary, 4)
        previously = QVBoxLayout()
        previously.addWidget(_label("PREVIOUSLY… · LAST RECORDED COMMIT", eyebrow=True))
        self.feature_previous = _label()
        self.feature_previous.setMinimumHeight(self.feature_previous.fontMetrics().lineSpacing() * 5)
        self.feature_previous.setAlignment(Qt.AlignmentFlag.AlignTop)
        previously.addWidget(self.feature_previous)
        previously.addStretch()
        story.addLayout(previously, 4)
        layout.addLayout(story)
        actions = QHBoxLayout()
        self.feature_pin = QPushButton("Pin")
        self.feature_pin.clicked.connect(self._pin_featured)
        actions.addWidget(self.feature_pin)
        actions.addStretch()
        self.feature_inspect = QPushButton("View project")
        self.feature_inspect.clicked.connect(self._inspect_featured)
        actions.addWidget(self.feature_inspect)
        self.feature_brief = QPushButton("Catch me up with AI…")
        self.feature_brief.setProperty("accent", True)
        self.feature_brief.clicked.connect(self._brief_featured)
        actions.addWidget(self.feature_brief)
        layout.addLayout(actions)
        return panel

    def _current_featured(self) -> dict | None:
        return self._featured[self._featured_index] if self._featured else None

    def _refresh_featured(self):
        current = self._current_featured()
        self._featured = self.model.featured_projects(self._pinned_paths)
        self._featured_index = next(
            (i for i, p in enumerate(self._featured) if current and p["path"] == current["path"]), 0
        )
        self._render_featured()

    def _step_featured(self, step: int):
        if self._featured:
            self._featured_index = (self._featured_index + step) % len(self._featured)
            self._render_featured()

    def _render_featured(self):
        from .ai import is_configured
        project = self._current_featured()
        self.feature_prev.setEnabled(len(self._featured) > 1)
        self.feature_next.setEnabled(len(self._featured) > 1)
        self.feature_inspect.setEnabled(project is not None)
        self.feature_pin.setEnabled(project is not None)
        self.feature_brief.setEnabled(project is not None and is_configured(self.cfg))
        self.feature_brief.setToolTip(
            "Request a fresh briefing from project files and recent git history"
            if is_configured(self.cfg) else "Configure a provider in Settings → AI"
        )
        if not project:
            self.feature_position.setText("0 / 0")
            self.feature_name.setText("Your next discovery")
            self.feature_meta.setText("Scan to bring your projects into view.")
            self.feature_summary.setText("Saved AI descriptions will appear here.")
            self.feature_previous.setText("Recent commit context, without the archaeology.")
            self.feature_pin.setText("Pin")
            return
        self.feature_position.setText(f"{self._featured_index + 1} / {len(self._featured)}")
        self.feature_name.setText(self._excerpt(project_label(project), 55))
        self.feature_name.setToolTip(f"{project_label(project)}\n{project['path']}")
        self.feature_meta.setText(" · ".join(str(v) for v in (
            project.get("language"), project.get("branch"),
            "Pinned" if project["path"] in self._pinned_paths else "Recently active",
        ) if v))
        description = project.get("description") or "No saved summary yet. Request an AI catch-up to explore this project."
        previous = project.get("last_commit_msg") or "No commit recorded. This may be a loose project or an empty repository."
        if project.get("last_commit_date"):
            previous = f"{project['last_commit_date'][:10]} — {previous}"
        self.feature_summary.setText(self._excerpt(description, 180))
        self.feature_summary.setToolTip(description)
        self.feature_previous.setText(self._excerpt(previous, 180))
        self.feature_previous.setToolTip(previous)
        self.feature_pin.setText("Unpin" if project["path"] in self._pinned_paths else "Pin")

    @staticmethod
    def _excerpt(text: str, limit: int) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[:limit - 1] + "…"

    def _toggle_pin(self, path: str):
        pins = self._pinned_paths ^ {path}
        conn = db.connect()
        try:
            db.kv_set(conn, "featured_pins", json.dumps(sorted(pins)))
            conn.commit()
        finally:
            conn.close()
        self._pinned_paths = pins
        self._refresh_featured()
        self._update_inspector()

    def _pin_selected(self):
        path = self._selected_path()
        if path:
            self._toggle_pin(path)

    def _pin_featured(self):
        project = self._current_featured()
        if project:
            self._toggle_pin(project["path"])

    def _inspect_featured(self):
        project = self._current_featured()
        if project:
            self._reset_filters()
            for row in range(self.proxy.rowCount()):
                if self.proxy.index(row, 0).data(PATH_ROLE) == project["path"]:
                    self.table.selectRow(row)
                    self.table.scrollTo(self.proxy.index(row, 0))
                    break
            self.details_btn.setChecked(True)

    def _brief_featured(self):
        project = self._current_featured()
        if project:
            self._ask_about(project["path"], (
                "Catch me up on this project. Briefly cover its purpose, what the recent commits "
                "and working tree show was being worked on, and any documented TODOs or next steps. "
                "Separate implemented work from plans. Cite files or commits and do not invent "
                "previous conversations or work that is not in the supplied context."
            ))

    def _set_view(self, view: str):
        self._view = view
        self.nav_buttons[view].setChecked(True)
        self.page_title.setText(PROJECT_VIEWS[view][0])
        self.page_subtitle.setText(PROJECT_VIEWS[view][1])
        self.proxy.set_view(view)
        self._update_count()
        self._update_inspector()

    def _filter_changed(self, text: str):
        self.proxy.set_needle(text)
        self._update_count()
        self._update_inspector()

    def _filter_updates_only(self, on: bool):
        self.proxy.set_updates_only(on)
        self._update_count()
        self._update_inspector()

    def _reset_filters(self):
        self.search.clear()
        self.updates_filter.setChecked(False)
        self._set_view("all")

    def _update_inspector(self, *_args):
        from .ai import is_configured
        indexes = self.table.selectionModel().selectedRows()
        proj = self.model.row(self.proxy.mapToSource(indexes[0]).row()) if indexes else None
        ready = bool(proj and not proj.get("missing"))
        self.open_btn.setEnabled(ready and self.launcher_combo.count() > 0)
        self.launcher_combo.setEnabled(ready and self.launcher_combo.count() > 0)
        self.project_ask_btn.setEnabled(ready and is_configured(self.cfg))
        self.project_ask_btn.setToolTip(
            "Ask using project files and git history" if is_configured(self.cfg)
            else "Configure a provider in Settings → AI"
        )
        self.copy_path_btn.setEnabled(proj is not None)
        self.pin_btn.setEnabled(proj is not None)
        self.pin_btn.setText(
            "Unpin" if proj and proj["path"] in self._pinned_paths else "Pin"
        )
        if not proj:
            self.detail_title.setText("Pick a project")
            self.detail_meta.setText("A little context goes a long way.")
            self.detail_description.setText("Select a row to see its story, open your tools or ask AI.")
            self.detail_path.setText("No project selected")
            self.detail_commit.setText("No project selected")
            return
        self.detail_title.setText(project_label(proj))
        state = "Missing on last scan" if proj.get("missing") else (
            "Uncommitted changes" if proj.get("dirty") else
            "Clean working tree" if proj.get("kind") == "git" else "No working tree"
        )
        self.detail_meta.setText(" · ".join(
            str(v) for v in (proj.get("language"), proj.get("branch"), state) if v
        ))
        self.detail_description.setText(proj.get("description") or "No description yet. Ask AI to explore this project.")
        self.detail_path.setText(proj["path"])
        self.detail_commit.setText(proj.get("last_commit_msg") or "No commit recorded.")

    def _open_selected(self):
        path = self._selected_path()
        name = self.launcher_combo.currentText()
        if path and name:
            launchers.launch(self.cfg, name, path)

    def _ask_selected(self):
        path = self._selected_path()
        if path:
            self._ask_custom(path)

    def _copy_selected_path(self):
        path = self._selected_path()
        if path:
            QGuiApplication.clipboard().setText(path)

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
        path = self._selected_path()
        self.cfg = load_config()
        self.model.refresh()
        chosen_launcher = self.launcher_combo.currentText()
        self.launcher_combo.clear()
        self.launcher_combo.addItems(launchers.launcher_names(self.cfg))
        if chosen_launcher in launchers.launcher_names(self.cfg):
            self.launcher_combo.setCurrentText(chosen_launcher)
        for row in range(self.proxy.rowCount()):
            if self.proxy.index(row, 0).data(PATH_ROLE) == path:
                self.table.selectRow(row)
                break
        self._update_count()
        self._update_inspector()
        self._refresh_featured()
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
        counts = self.model.view_counts()
        for view, button in self.nav_buttons.items():
            button.setText(f"{PROJECT_VIEWS[view][0]}  ·  {counts[view]}")
        for view, (button, value, title) in self.metric_buttons.items():
            value.setText(str(counts[view]))
            button.setAccessibleName(f"{counts[view]} {title.lower()}")
        has_rows = self.proxy.rowCount() > 0
        self.results.setCurrentIndex(0 if has_rows else 1)
        self.empty_title.setText("Nothing on the radar yet" if not self.model.rowCount() else "No matching projects")
        self.empty_help.setText(
            "Choose your search roots in Settings, then Scan Now."
            if not self.model.rowCount() else "Try a different search or catalogue view."
        )
        self._set_update_button_state(
            self.model.updates_count(self.cfg.self_repo))

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
        proc.errorOccurred.connect(
            lambda error: self._task_start_error(error, proc, attr, btn, idle, args[0])
        )
        setattr(self, attr, proc)
        self.activity.appendPlainText(f"$ codehound {' '.join(args)}")
        proc.start(sys.executable, ["-m", "codehound.cli", *args])

    def _task_start_error(self, error, proc, attr, btn, idle, what):
        if error == QProcess.ProcessError.FailedToStart:
            self._task_output(f"{what} could not start: {proc.errorString()}")
            self._task_finished(attr, -1, btn, idle, what)

    def start_scan(self):
        self._start_cli("_scan_proc", ["scan"], self.scan_btn, "Scanning…", "Scan Now")

    def start_sync(self):
        self._start_cli("_sync_proc", ["sync"], self.sync_btn, "Syncing…", "Sync Accounts")

    def start_updates(self):
        self._start_cli("_updates_proc", ["updates", "--check"],
                        self.updates_btn, "Checking…", "Check Updates")

    def install_all_updates(self):
        """Confirm the pending updates, then install every one in a single run."""
        if self._updates_proc is not None:
            return
        try:
            conn = db.connect()
            try:
                rows = db.available_updates(conn)
            finally:
                conn.close()
        except sqlite3.Error:
            return
        rows = [r for r in rows if r["repo"] != self.cfg.self_repo]
        if not rows:
            self.statusBar().showMessage("install updates: nothing pending")
            return
        lines = []
        for r in rows[:12]:
            have = r["current_tag"] or "unknown"
            lines.append(f"  • {r['name'] or r['repo']}  {have} → {r['latest_tag']}")
        if len(rows) > 12:
            lines.append(f"  • …and {len(rows) - 12} more")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Install updates")
        box.setText(f"Install {len(rows)} update(s)?")
        box.setInformativeText("\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.Yes)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self._start_cli("_updates_proc", ["updates", "--install-all"],
                        self.install_updates_btn, "Installing…", "Install Updates")

    def _install_one_update(self, repo: str):
        """Context menu: install just this app's newest release."""
        if self._updates_proc is not None:
            return
        self._start_cli("_updates_proc", ["update", repo],
                        self.updates_btn, "Installing…", "Check Updates")

    def _update_settings(self, repo: str, name: str, path: str = ""):
        dlg = _UpdateSettingsDialog(self.cfg, repo, name, path, self)
        if dlg.exec() == _UpdateSettingsDialog.DialogCode.Accepted:
            self.reload()
            self.statusBar().showMessage(f"update settings saved for {repo}")

    def _watch_repo(self, proj: dict | None, update: dict | None) -> str:
        """Repo whose update settings apply: the watched row's when one
        exists, else the project's GitHub remote ('' = not watchable, and
        never codeHOUND itself — that goes through 'Update codeHOUND…')."""
        if update:
            return "" if update["repo"] == self.cfg.self_repo else update["repo"]
        if not proj:
            return ""
        from .updates import parse_github_repo
        repo = parse_github_repo(proj.get("remote_url") or "") or ""
        return "" if repo == self.cfg.self_repo else repo

    def _set_update_button_state(self, n_updates: int):
        """Install Updates is only enabled when something is pending."""
        btn = self.install_updates_btn
        if n_updates <= 0:
            btn.setEnabled(False)
            btn.setToolTip("No updates available yet")
        else:
            btn.setEnabled(self._updates_proc is None)
            btn.setToolTip(
                f"Install {n_updates} available update(s) — "
                "each app's Update Settings decide where and how"
            )
        if n_updates:
            self.updates_filter.setToolTip(
                f"Show only the {n_updates} project(s) with a newer release available"
            )
        else:
            self.updates_filter.setToolTip(
                "Show only projects with a newer release available"
            )

    def start_self_update(self):
        """Update codeHOUND itself, then quit and come back on the new build."""
        if self._updates_proc is not None:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Update codeHOUND")
        box.setText("Update codeHOUND to its newest release?\n\n"
                    "A system package install will ask for admin rights (pkexec). "
                    "codeHOUND quits and restarts on the new version once the "
                    "install finishes.")
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self._start_cli("_updates_proc", ["self-update"], self.updates_btn,
                        "Updating…", "Check Updates")

    def _relaunch_after_self_update(self):
        """Hand the restart to a detached helper, then quit so it can run."""
        from . import updates
        try:
            updates.schedule_relaunch(
                when_pid=os.getpid(),
                launch=[sys.executable, "-m", "codehound.cli", "app"],
                progress=lambda m: self._task_output(m))
        except OSError as e:
            self._task_output(f"relaunch failed: {e} — start codeHOUND again manually")
            return
        app = QApplication.instance()
        if app is not None:
            app.quit()

    def new_project(self, default_tab: int = 0):
        dlg = NewProjectDialog(self.cfg, self, default_tab, self._selected_path())
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._start_cli("_new_proc", dlg.args(), self.new_btn, "Working…", "New Project…")

    def refresh_digest(self):
        if not self.cfg.ai_catalogue_summary:
            self.statusBar().showMessage(
                "enable catalogue summaries in Settings → AI first")
            return
        self._start_cli("_digest_proc", ["ask", "--refresh-digest"],
                        self.ask_btn, "Summarising…", "Ask AI…")

    def _task_output(self, text: str):
        self.activity.appendPlainText(text.rstrip())
        line = text.strip().splitlines()
        if line:
            self.statusBar().showMessage(line[-1][:200])

    def _task_finished(self, attr: str, code: int, btn: QPushButton, idle: str, what: str):
        proc = getattr(self, attr)
        setattr(self, attr, None)
        if proc is not None:
            proc.deleteLater()
        btn.setEnabled(True)
        btn.setText(idle)
        self.reload()
        self.activity.appendPlainText(f"{what} finished with exit code {code}")
        if code != 0:
            self.activity_dock.show()
            self.statusBar().showMessage(f"{what} exited with code {code} — see Activity")
        elif what == "updates":
            self._notify_updates()
        elif what == "self-update":
            self._relaunch_after_self_update()

    def _notify_updates(self):
        """Bubble a tray message when an update check found something new.

        Updates the auto policy staged (downloaded + verified) get a
        click-to-install message; `activate_staged` installs them."""
        try:
            conn = db.connect()
            try:
                rows = db.available_updates(conn)
                staged = db.staged_updates(conn)
            finally:
                conn.close()
        except sqlite3.Error:
            return
        self._staged_count = sum(1 for r in staged if (r["staged_dir"] or ""))
        # codeHOUND's own row installs through 'Update codeHOUND…', never the
        # Install Updates button — keep it out of that flow but still mention it.
        mine = (self.cfg.self_repo or "").strip()
        self_row = next((r for r in rows if r["repo"] == mine), None) if mine else None
        rows = [r for r in rows if r["repo"] != mine]
        if not rows and self_row is None:
            self.statusBar().showMessage("updates: everything is current")
            return
        parts = [f"{r['name'] or r['repo']} → {r['latest_tag']}" for r in rows[:3]]
        if len(rows) > 3:
            parts.append(f"…and {len(rows) - 3} more")
        if self_row is not None:
            parts.append(f"codeHOUND → {self_row['latest_tag']} "
                         "(use 'Update codeHOUND…' in the tray menu)")
        text = "\n".join(parts)
        if rows:
            text += ("\n\nClick here to install the staged downloads."
                     if self._staged_count else
                     "\n\nUse Install Updates to apply them.")
            self.statusBar().showMessage(
                f"{len(rows)} update(s) available — Install Updates is ready"
            )
        else:
            self.statusBar().showMessage(
                "codeHOUND has an update — use 'Update codeHOUND…'")
        if self.tray is not None:
            n = len(rows) + (1 if self_row is not None else 0)
            self.tray.showMessage(
                f"codeHOUND — {n} update(s) available", text,
                QSystemTrayIcon.MessageIcon.Information, 10000,
            )

    def activate_staged(self):
        """Tray message click: install the updates staging already verified."""
        if not self._staged_count or self._updates_proc is not None:
            return
        self._staged_count = 0
        self._start_cli("_updates_proc", ["updates", "--activate"],
                        self.updates_btn, "Installing…", "Check Updates")

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
        menu = self._build_context_menu(pos)
        if menu is not None:
            menu.exec(self.table.viewport().mapToGlobal(pos))

    def _build_context_menu(self, pos) -> QMenu | None:
        index = self.table.indexAt(pos)
        path = self._selected_path(index)
        if not path:
            return None
        menu = QMenu(self)
        for name in launchers.launcher_names(self.cfg):
            act = menu.addAction(f"Open in {name}")
            act.triggered.connect(lambda _=False, n=name, p=path: launchers.launch(self.cfg, n, p))
        menu.addSeparator()
        proj = self.model.row(self.proxy.mapToSource(index).row()) if index.isValid() else None
        update = self.model.update_for(proj) if proj else None
        if update:
            act = menu.addAction(
                f"Install Update — {update['latest_tag']}"
            )
            if update["repo"] == self.cfg.self_repo:
                act.setToolTip(
                    f"codeHOUND {update['current_tag'] or 'unknown'} → "
                    f"{update['latest_tag']} — quits and restarts on the new build"
                )
                act.triggered.connect(lambda _=False: self.start_self_update())
            else:
                act.setToolTip(
                    f"{update['repo']}: {update['current_tag'] or 'unknown'} → "
                    f"{update['latest_tag']}"
                )
                act.triggered.connect(
                    lambda _=False, r=update["repo"]: self._install_one_update(r)
                )
        watch_repo = self._watch_repo(proj, update)
        if proj and watch_repo:
            act = menu.addAction("Update Settings…")
            act.setToolTip("Per-app install path and auto-install policy")
            act.triggered.connect(
                lambda _=False, r=watch_repo, n=proj["name"], p=proj["path"]:
                self._update_settings(r, n, p)
            )
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
        menu.addAction("Add worktree…").triggered.connect(lambda: self.new_project(default_tab=2))
        menu.addAction("Copy path").triggered.connect(
            lambda: QGuiApplication.clipboard().setText(path)
        )
        return menu

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
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "metrics_panel"):
            self.metrics_panel.setVisible(event.size().height() >= 820)

    def closeEvent(self, event):
        event.ignore()
        self.hide()


def run_app() -> int:
    app = QApplication(sys.argv)
    apply_theme(app)
    app.setApplicationName("codeHOUND")
    app.setDesktopFileName("codehound")
    app.setQuitOnLastWindowClosed(False)
    # The tray icon must come from the icon theme: StatusNotifier sends the
    # icon by *name* over D-Bus, and a QIcon built from a lone file path has
    # no name — so the panel got an empty IconName and rendered nothing.
    # QIcon.fromTheme(name, fallback) gives us a named icon with Cody as
    # the fallback pixmap for environments without the theme icon installed.
    icon = QIcon.fromTheme("codehound-cody", QIcon(str(ICON_PATH)))
    app.setWindowIcon(icon)

    win = Dashboard()

    tray = QSystemTrayIcon(icon)
    tray.setToolTip(f"codeHOUND {__version__} — repo hunter")
    menu = QMenu()
    from .ai import is_configured
    ai_ready = is_configured(win.cfg)
    version_act = menu.addAction(f"codeHOUND v{__version__}")
    version_act.setEnabled(False)
    menu.addSeparator()
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
    if (win.cfg.self_repo or "").strip():
        menu.addAction("Update codeHOUND…").triggered.connect(win.start_self_update)
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
    # clicking an update notification activates (installs) the staged updates
    tray.messageClicked.connect(win.activate_staged)
    tray.show()
    win.tray = tray

    win.show()
    return app.exec()
