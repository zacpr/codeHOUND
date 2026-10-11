"""Settings dialog — edit codeHOUND configuration from the GUI."""
from __future__ import annotations

from functools import partial

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import discovery
from .ai import AIError
from .ai import PROVIDERS as AI_PROVIDERS
from .ai import validate_base_url
from .config import Config, save_config
from .providers import PROVIDERS
from .providers.azure import AzureAccount
from .providers.github import GitHubAccount


def _pick_detected(parent, title: str, label: str, items: list[str]) -> list[int]:
    """Show a checklist dialog (all pre-checked); return indices left checked, or [] on Cancel."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(title)
    dlg.resize(480, 320)
    layout = QVBoxLayout(dlg)
    layout.addWidget(QLabel(label))
    listw = QListWidget()
    for text in items:
        item = QListWidgetItem(text)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked)
        listw.addItem(item)
    layout.addWidget(listw, 1)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.accepted.connect(dlg.accept)
    buttons.rejected.connect(dlg.reject)
    layout.addWidget(buttons)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return []
    return [i for i in range(listw.count()) if listw.item(i).checkState() == Qt.CheckState.Checked]


class _GitHubAccountDialog(QDialog):
    def __init__(self, account: GitHubAccount | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GitHub Account")
        form = QFormLayout(self)
        self.user = QLineEdit()
        self.user.setPlaceholderText("display label — the authenticated user is mirrored either way")
        form.addRow("Username:", self.user)
        self.token = QLineEdit()
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setPlaceholderText("blank = use the gh CLI login")
        form.addRow("Token (PAT):", self.token)
        self.orgs = QLineEdit()
        self.orgs.setPlaceholderText("comma-separated, e.g. myorg, anotherorg")
        form.addRow("Organisations:", self.orgs)
        self.forks = QCheckBox("Also mirror forks")
        form.addRow(self.forks)
        self.archived = QCheckBox("Also mirror archived repos")
        form.addRow(self.archived)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        if account:
            self.user.setText(account.user)
            self.token.setText(account.token)
            self.orgs.setText(", ".join(account.orgs))
            self.forks.setChecked(account.forks)
            self.archived.setChecked(account.archived)

    def account(self) -> GitHubAccount:
        return GitHubAccount(
            user=self.user.text().strip(),
            token=self.token.text().strip(),
            orgs=[o.strip() for o in self.orgs.text().split(",") if o.strip()],
            forks=self.forks.isChecked(),
            archived=self.archived.isChecked(),
        )


class _AzureAccountDialog(QDialog):
    def __init__(self, account: AzureAccount | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Azure DevOps Account")
        form = QFormLayout(self)
        self.org = QLineEdit()
        self.org.setPlaceholderText("dev.azure.com/<org>")
        form.addRow("Organisation:", self.org)
        self.pat = QLineEdit()
        self.pat.setEchoMode(QLineEdit.EchoMode.Password)
        self.pat.setPlaceholderText("blank = use the Azure CLI (az) login")
        form.addRow("PAT:", self.pat)
        self.projects = QLineEdit()
        self.projects.setPlaceholderText("comma-separated; blank = all projects")
        form.addRow("Projects:", self.projects)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        if account:
            self.org.setText(account.org)
            self.pat.setText(account.pat)
            self.projects.setText(", ".join(account.projects))

    def account(self) -> AzureAccount:
        return AzureAccount(
            org=self.org.text().strip(),
            pat=self.pat.text().strip(),
            projects=[p.strip() for p in self.projects.text().split(",") if p.strip()],
        )


# provider key -> account editor dialog; new providers register their dialog here
ACCOUNT_EDITORS = {
    "github": _GitHubAccountDialog,
    "azure": _AzureAccountDialog,
}


class _AccountsEditor(QWidget):
    """List of configured provider accounts with add/edit/remove."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._accounts: list[tuple[str, object]] = []  # (provider key, account)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            QLabel("`codehound sync` keeps a local clone of every repo in these accounts. "
                   "Tokens stay in config.toml; leave blank to use the gh / az CLI logins.")
        )
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        btns = QHBoxLayout()
        for key, editor in ACCOUNT_EDITORS.items():
            btn = QPushButton(f"Add {PROVIDERS[key].label}…")
            btn.clicked.connect(lambda _=False, k=key, e=editor: self._add(k, e))
            btns.addWidget(btn)
        edit_btn = QPushButton("Edit")
        remove_btn = QPushButton("Remove")
        edit_btn.clicked.connect(self._edit)
        remove_btn.clicked.connect(self._remove)
        btns.addWidget(edit_btn)
        btns.addWidget(remove_btn)
        btns.addStretch(1)
        layout.addLayout(btns)
        self.list.itemDoubleClicked.connect(lambda _item: self._edit())

    def _label(self, key: str, account) -> str:
        return f"{PROVIDERS[key].label}: {PROVIDERS[key].account_label(account)}"

    def _refresh(self):
        self.list.clear()
        for key, account in self._accounts:
            self.list.addItem(self._label(key, account))

    def _add(self, key: str, editor):
        dlg = editor(parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._accounts.append((key, dlg.account()))
            self._refresh()

    def _edit(self):
        row = self.list.currentRow()
        if row < 0:
            return
        key, account = self._accounts[row]
        dlg = ACCOUNT_EDITORS[key](account, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._accounts[row] = (key, dlg.account())
            self._refresh()
            self.list.setCurrentRow(row)

    def _remove(self):
        row = self.list.currentRow()
        if row >= 0:
            del self._accounts[row]
            self._refresh()

    def set_values(self, accounts: dict[str, list]):
        self._accounts = [
            (key, account)
            for key in PROVIDERS
            for account in accounts.get(key, [])
        ]
        self._refresh()

    def values(self) -> dict[str, list]:
        out: dict[str, list] = {key: [] for key in PROVIDERS}
        for key, account in self._accounts:
            out[key].append(account)
        return out


class _PathListEditor(QWidget):
    """List of strings with add/edit/remove buttons and an optional dir picker."""

    def __init__(self, title: str, browse_dirs: bool = False, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(title))
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        btns = QHBoxLayout()
        add_btn = QPushButton("Add…" if browse_dirs else "Add")
        edit_btn = QPushButton("Edit")
        remove_btn = QPushButton("Remove")
        btns.addWidget(add_btn)
        btns.addWidget(edit_btn)
        btns.addWidget(remove_btn)
        btns.addStretch(1)
        layout.addLayout(btns)

        if browse_dirs:
            add_btn.clicked.connect(self._browse)
        else:
            add_btn.clicked.connect(lambda: self._edit_item(None))
        edit_btn.clicked.connect(lambda: self._edit_item(self.list.currentItem()))
        remove_btn.clicked.connect(self._remove)
        self.list.itemDoubleClicked.connect(self._edit_item)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Add directory")
        if path:
            self.list.addItem(path)

    def _edit_item(self, item):
        current = item.text() if item else ""
        text, ok = QInputDialog.getText(self, "Entry", "Value:", text=current)
        text = text.strip()
        if not ok or not text:
            return
        if item:
            item.setText(text)
        else:
            self.list.addItem(text)

    def _remove(self):
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))

    def set_values(self, values: list[str]):
        self.list.clear()
        self.list.addItems(values)

    def values(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]


class _LaunchersEditor(QWidget):
    """Ordered name → command table; the first row is the double-click default."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(
            QLabel("Shown in the context menu in this order. The first entry is the "
                   "double-click default. Use {path} where the project path goes.")
        )
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Name", "Command"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        layout.addWidget(self.table, 1)
        btns = QHBoxLayout()
        add_btn = QPushButton("Add")
        remove_btn = QPushButton("Remove")
        up_btn = QPushButton("Move Up")
        down_btn = QPushButton("Move Down")
        detect_btn = QPushButton("Detect installed tools…")
        for b in (add_btn, remove_btn, up_btn, down_btn, detect_btn):
            btns.addWidget(b)
        btns.addStretch(1)
        layout.addLayout(btns)

        add_btn.clicked.connect(lambda: self._insert_row(self.table.rowCount()))
        remove_btn.clicked.connect(self._remove)
        up_btn.clicked.connect(lambda: self._move(-1))
        down_btn.clicked.connect(lambda: self._move(1))
        detect_btn.clicked.connect(self._detect)

    def _detect(self):
        found = discovery.detect_editors(self.values()) + discovery.detect_agent_clis(self.values())
        if not found:
            QMessageBox.information(
                self, "Detect installed tools",
                "No new editors, file managers or AI agent CLIs were found on PATH."
            )
            return
        labels = [f"{d.name} — {d.command}" for d in found]
        for i in _pick_detected(
            self, "Detect installed tools",
            "Found on this machine. Tick the ones to add as launchers:", labels,
        ):
            self._insert_row(self.table.rowCount(), found[i].name, found[i].command)

    def _insert_row(self, row: int, name: str = "", cmd: str = ""):
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(name))
        self.table.setItem(row, 1, QTableWidgetItem(cmd))
        self.table.setCurrentCell(row, 0)

    def _remove(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            self.table.removeRow(row)

    def _move(self, delta: int):
        row = self.table.currentRow()
        target = row + delta
        if row < 0 or not (0 <= target < self.table.rowCount()):
            return
        for col in range(2):
            a = self.table.takeItem(row, col)
            b = self.table.takeItem(target, col)
            self.table.setItem(row, col, b)
            self.table.setItem(target, col, a)
        self.table.setCurrentCell(target, 0)

    def set_values(self, launchers: dict[str, str]):
        self.table.setRowCount(0)
        for name, cmd in launchers.items():
            self._insert_row(self.table.rowCount(), name, cmd)

    def values(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            cmd_item = self.table.item(row, 1)
            name = name_item.text().strip() if name_item else ""
            cmd = cmd_item.text().strip() if cmd_item else ""
            if name and cmd:
                out[name] = cmd
        return out


class SettingsDialog(QDialog):
    def __init__(self, cfg: Config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("codeHOUND Settings")
        self.resize(700, 560)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        # --- Scan ---
        scan_tab = QWidget()
        scan = QVBoxLayout(scan_tab)
        self.roots = _PathListEditor("Search roots — directories to hunt for projects:", browse_dirs=True)
        self.exclude_dirs = _PathListEditor("Exclude directory names — pruned anywhere in the tree:")
        self.exclude_paths = _PathListEditor("Exclude paths — absolute paths pruned exactly:", browse_dirs=True)
        scan.addWidget(self.roots, 1)
        scan.addWidget(self.exclude_dirs, 1)
        scan.addWidget(self.exclude_paths, 1)
        opts = QFormLayout()
        self.max_depth = QSpinBox()
        self.max_depth.setRange(1, 64)
        opts.addRow("Max depth:", self.max_depth)
        self.one_filesystem = QCheckBox("Don't cross mount points inside a root")
        opts.addRow(self.one_filesystem)
        scan.addLayout(opts)
        tabs.addTab(scan_tab, "Scan")

        # --- Ollama ---
        ollama_tab = QWidget()
        form = QFormLayout(ollama_tab)
        self.ollama_enabled = QCheckBox("Use Ollama for descriptions / classification")
        form.addRow(self.ollama_enabled)
        self.ollama_url = QLineEdit()
        form.addRow("Server URL:", self.ollama_url)
        self.ollama_model = QLineEdit()
        form.addRow("Model:", self.ollama_model)
        self.ollama_enrich = QCheckBox("One-line AI project descriptions")
        form.addRow(self.ollama_enrich)
        self.ollama_classify = QCheckBox("LLM decides if ambiguous dirs are projects")
        form.addRow(self.ollama_classify)
        self.ollama_timeout = QSpinBox()
        self.ollama_timeout.setRange(1, 600)
        self.ollama_timeout.setSuffix(" s")
        form.addRow("Request timeout:", self.ollama_timeout)
        tabs.addTab(ollama_tab, "Ollama")

        # --- AI (ask) ---
        ai_tab = QWidget()
        ai_form = QFormLayout(ai_tab)
        ai_form.addRow(QLabel(
            "Model used by `codehound ask` and the dashboard's Ask AI actions. "
            "(The Ollama tab configures scan-time enrichment separately.)"
        ))
        self.ai_provider = QComboBox()
        for key, provider in AI_PROVIDERS.items():
            self.ai_provider.addItem(provider.label, key)
        ai_form.addRow("Provider:", self.ai_provider)
        self.ai_model = QLineEdit()
        ai_form.addRow("Model:", self.ai_model)
        self.ai_api_key = QLineEdit()
        self.ai_api_key.setEchoMode(QLineEdit.EchoMode.Password)
        ai_form.addRow("API key:", self.ai_api_key)
        self.ai_base_url = QLineEdit()
        ai_form.addRow("Base URL:", self.ai_base_url)
        detect_ai_btn = QPushButton("Detect local AI services…")
        detect_ai_btn.clicked.connect(self._detect_ai)
        ai_form.addRow(detect_ai_btn)
        self.ai_catalogue_summary = QCheckBox(
            "Model-written catalogue summary — after each scan the model writes a\n"
            "compact digest; catalogue questions answer from it instead of the raw\n"
            "list. Flagged stale when a project's folder changes afterwards."
        )
        ai_form.addRow(self.ai_catalogue_summary)
        self.ai_provider.currentIndexChanged.connect(self._ai_placeholders)
        self._ai_placeholders()
        tabs.addTab(ai_tab, "AI")

        # --- Launchers ---
        launchers_tab = QWidget()
        launchers_layout = QVBoxLayout(launchers_tab)
        self.launchers = _LaunchersEditor()
        launchers_layout.addWidget(self.launchers)
        tabs.addTab(launchers_tab, "Launchers")

        # --- Accounts ---
        accounts_tab = QWidget()
        accounts_layout = QVBoxLayout(accounts_tab)
        self.accounts = _AccountsEditor()
        accounts_layout.addWidget(self.accounts, 1)
        clone_form = QFormLayout()
        clone_row = QHBoxLayout()
        self.clone_dest = QLineEdit()
        browse_clone = QPushButton("Browse…")
        # partial over a bound method is weakly referenced — a lambda capturing
        # self here would cycle (dialog -> button -> lambda -> dialog) and leak
        browse_clone.clicked.connect(partial(self._browse_into, self.clone_dest, "Clone destination"))
        clone_row.addWidget(self.clone_dest, 1)
        clone_row.addWidget(browse_clone)
        clone_holder = QWidget()
        clone_holder.setLayout(clone_row)
        clone_form.addRow("Clone destination:", clone_holder)
        accounts_layout.addLayout(clone_form)
        tabs.addTab(accounts_tab, "Accounts")

        # --- Adopt ---
        adopt_tab = QWidget()
        adopt_form = QFormLayout(adopt_tab)
        dest_row = QHBoxLayout()
        self.adopt_dest = QLineEdit()
        browse_dest = QPushButton("Browse…")
        browse_dest.clicked.connect(partial(self._browse_into, self.adopt_dest, "Adopt destination"))
        dest_row.addWidget(self.adopt_dest, 1)
        dest_row.addWidget(browse_dest)
        dest_holder = QWidget()
        dest_holder.setLayout(dest_row)
        adopt_form.addRow("Adopt destination:", dest_holder)
        tabs.addTab(adopt_tab, "Adopt")

        # --- Janitor ---
        janitor_tab = QWidget()
        janitor_layout = QVBoxLayout(janitor_tab)
        janitor_layout.addWidget(QLabel(
            "Removes local clones whose remote work is done (PR merged/abandoned,\n"
            "repo archived/disabled). Only clean, fully-pushed clones are removed.\n"
            "Always dry-run by default — use `codehound janitor --force` to apply."
        ))
        self.janitor_enabled = QCheckBox("Enable janitor")
        janitor_layout.addWidget(self.janitor_enabled)
        self.janitor_exclude = _PathListEditor(
            "Never touch these (repo names or full paths):"
        )
        janitor_layout.addWidget(self.janitor_exclude, 1)
        run_btn = QPushButton("Run Janitor (dry-run)…")
        run_btn.clicked.connect(self._run_janitor)
        janitor_layout.addWidget(run_btn)
        tabs.addTab(janitor_tab, "Janitor")

        # --- Updates ---
        updates_tab = QWidget()
        updates_form = QFormLayout(updates_tab)
        updates_form.addRow(QLabel(
            "Watches GitHub releases for catalogued projects and offers to\n"
            "install newer ones (Check Updates / Install Updates in the app,\n"
            "or `codehound updates` on the command line)."
        ))
        self.updates_enabled = QCheckBox("Watch for updates")
        updates_form.addRow(self.updates_enabled)
        self.updates_mode = QComboBox()
        self.updates_mode.addItem("Ask before installing (stage + confirm)", "stage")
        self.updates_mode.addItem("Install automatically, notify after", "silent")
        updates_form.addRow("When an update appears:", self.updates_mode)
        self.updates_own_only = QCheckBox(
            "Only repos I own myself — skip orgs and forks"
        )
        self.updates_own_only.setToolTip(
            "Needs a GitHub login (gh CLI or a token in Settings → Accounts) to\n"
            "tell your repos from everyone else's. Unauthenticated falls back\n"
            "to watching every catalogued GitHub project."
        )
        updates_form.addRow(self.updates_own_only)
        install_dir_row = QHBoxLayout()
        self.updates_install_dir = QLineEdit()
        self.updates_install_dir.setPlaceholderText(
            "default — replace in place, else a managed dir with a ~/.local/bin link")
        browse_upd = QPushButton("Browse…")
        browse_upd.clicked.connect(partial(self._browse_into, self.updates_install_dir,
                                           "Default install location for updates"))
        install_dir_row.addWidget(self.updates_install_dir, 1)
        install_dir_row.addWidget(browse_upd)
        install_dir_holder = QWidget()
        install_dir_holder.setLayout(install_dir_row)
        updates_form.addRow("Default install location:", install_dir_holder)
        self.self_repo = QLineEdit()
        self.self_repo.setPlaceholderText("owner/repo — blank turns self-updating off")
        self.self_repo.setToolTip("codeHOUND keeps itself current from this repo")
        updates_form.addRow("codeHOUND updates from:", self.self_repo)
        updates_form.addRow(QLabel(
            "Per-app overrides: right-click a project row → Update Settings…"
        ))
        tabs.addTab(updates_tab, "Updates")

        # --- buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load(cfg)

    def _ai_placeholders(self):
        provider = AI_PROVIDERS[self.ai_provider.currentData()]
        self.ai_model.setPlaceholderText(f"default: {provider.default_model}" if provider.default_model else "required")
        self.ai_base_url.setPlaceholderText(
            f"default: {provider.default_base_url}" if provider.default_base_url else "required"
        )
        self.ai_api_key.setPlaceholderText(
            f"blank = ${provider.env_key}" if provider.env_key else "not required"
        )

    def _detect_ai(self):
        from PySide6.QtWidgets import QApplication

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            found = discovery.detect_model_runners()
        finally:
            QApplication.restoreOverrideCursor()
        if not found:
            QMessageBox.information(
                self, "Detect local AI services",
                "No local AI services (LM Studio, OpenWebUI, text-generation-webui, …) "
                "were reachable. (Ollama has its own tab.)"
            )
            return
        labels = [
            f"{r.label} — {r.base_url}" + (f" (model: {r.model})" if r.model else "")
            for r in found
        ]
        picked = _pick_detected(
            self, "Detect local AI services",
            "Reachable right now. Pick the one to use for Ask AI:", labels,
        )
        if not picked:
            return
        chosen = found[picked[0]]
        idx = self.ai_provider.findData("custom")
        self.ai_provider.setCurrentIndex(idx if idx >= 0 else 0)
        self.ai_base_url.setText(chosen.base_url)
        if chosen.model:
            self.ai_model.setText(chosen.model)

    def _load(self, cfg: Config):
        self.roots.set_values(cfg.roots)
        self.exclude_dirs.set_values(cfg.exclude_dirs)
        self.exclude_paths.set_values(cfg.exclude_paths)
        self.max_depth.setValue(cfg.max_depth)
        self.one_filesystem.setChecked(cfg.one_filesystem)
        self.ollama_enabled.setChecked(cfg.ollama_enabled)
        self.ollama_url.setText(cfg.ollama_url)
        self.ollama_model.setText(cfg.ollama_model)
        self.ollama_enrich.setChecked(cfg.ollama_enrich)
        self.ollama_classify.setChecked(cfg.ollama_classify)
        self.ollama_timeout.setValue(cfg.ollama_timeout)
        self.launchers.set_values(cfg.launchers)
        idx = self.ai_provider.findData(cfg.ai_provider)
        self.ai_provider.setCurrentIndex(idx if idx >= 0 else 0)
        self.ai_model.setText(cfg.ai_model)
        self.ai_api_key.setText(cfg.ai_api_key)
        self.ai_base_url.setText(cfg.ai_base_url)
        self.ai_catalogue_summary.setChecked(cfg.ai_catalogue_summary)
        self.accounts.set_values(cfg.accounts)
        self.clone_dest.setText(cfg.clone_dest)
        self.janitor_enabled.setChecked(cfg.janitor_enabled)
        self.janitor_exclude.set_values(cfg.janitor_exclude)
        self.adopt_dest.setText(cfg.adopt_dest)
        self.updates_enabled.setChecked(cfg.updates_enabled)
        idx = self.updates_mode.findData(cfg.updates_mode)
        self.updates_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.updates_own_only.setChecked(cfg.updates_own_only)
        self.updates_install_dir.setText(cfg.updates_install_dir)
        self.self_repo.setText(cfg.self_repo)

    def _browse_into(self, target: QLineEdit, title: str):
        path = QFileDialog.getExistingDirectory(self, title, target.text())
        if path:
            target.setText(path)

    def _run_janitor(self):
        from PySide6.QtCore import QProcess
        import sys
        dlg = QDialog(self)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dlg.setWindowTitle("Janitor — dry run")
        dlg.resize(640, 420)
        layout = QVBoxLayout(dlg)
        out = QPlainTextEdit()
        out.setReadOnly(True)
        out.setPlainText("running janitor…\n")
        layout.addWidget(out)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)
        proc = QProcess(dlg)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(
            lambda: out.appendPlainText(proc.readAllStandardOutput().data().decode(errors="replace").rstrip("\n"))
        )
        proc.finished.connect(lambda code, _s: out.appendPlainText(
            f"\n(dry-run complete; use `codehound janitor --force` to remove)"
            if code == 0 else f"\n(janitor exited with code {code})"
        ))
        proc.start(sys.executable, ["-m", "codehound.cli", "janitor"])
        dlg.exec()

    def _save(self):
        roots = self.roots.values()
        launchers = self.launchers.values()
        if not roots:
            QMessageBox.warning(self, "Settings", "Add at least one search root.")
            return
        if not launchers:
            QMessageBox.warning(self, "Settings", "Add at least one launcher.")
            return
        ollama_url = self.ollama_url.text().strip() or "http://localhost:11434"
        ai_base_url = self.ai_base_url.text().strip()
        try:
            validate_base_url(ollama_url, hint="check the Ollama tab")
            if ai_base_url:
                validate_base_url(ai_base_url, hint="check the AI tab")
        except AIError as e:
            QMessageBox.warning(self, "Settings", str(e))
            return
        cfg = Config(
            roots=roots,
            exclude_dirs=self.exclude_dirs.values(),
            exclude_paths=self.exclude_paths.values(),
            max_depth=self.max_depth.value(),
            one_filesystem=self.one_filesystem.isChecked(),
            ollama_enabled=self.ollama_enabled.isChecked(),
            ollama_url=ollama_url,
            ollama_model=self.ollama_model.text().strip(),
            ollama_enrich=self.ollama_enrich.isChecked(),
            ollama_classify=self.ollama_classify.isChecked(),
            ollama_timeout=self.ollama_timeout.value(),
            launchers=launchers,
            adopt_dest=self.adopt_dest.text().strip(),
            clone_dest=self.clone_dest.text().strip(),
            accounts=self.accounts.values(),
            ai_provider=self.ai_provider.currentData(),
            ai_model=self.ai_model.text().strip(),
            ai_api_key=self.ai_api_key.text().strip(),
            ai_base_url=ai_base_url,
            ai_catalogue_summary=self.ai_catalogue_summary.isChecked(),
            janitor_enabled=self.janitor_enabled.isChecked(),
            janitor_exclude=self.janitor_exclude.values(),
            updates_enabled=self.updates_enabled.isChecked(),
            updates_mode=self.updates_mode.currentData(),
            updates_own_only=self.updates_own_only.isChecked(),
            updates_install_dir=self.updates_install_dir.text().strip(),
            self_repo=self.self_repo.text().strip(),
        )
        save_config(cfg)
        self.accept()
