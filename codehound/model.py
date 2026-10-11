"""Qt table model over the projects database."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPen
from PySide6.QtWidgets import QStyledItemDelegate, QTableView

from . import db

# Visible order first (Name, Branch, Description, Modified, Language, Status,
# ReleaseUpdated); the last four stay in the model for the Columns menu but
# are hidden by default.
COLUMNS = [
    ("Name", "name"),
    ("Branch", "branch"),
    ("Description", "description"),
    ("Modified", "modified"),
    ("Language", "language"),
    ("Status", "dirty"),
    ("Release Updated", "release"),
    ("Kind", "kind"),
    ("Created", "created"),
    ("Root", "root"),
    ("Path", "path"),
]
DEFAULT_HIDDEN_COLUMNS = (7, 8, 9, 10)
PATH_ROLE = Qt.ItemDataRole.UserRole + 1
UPDATE_ROLE = Qt.ItemDataRole.UserRole + 2   # available-update dict, or None
DATE_FIELDS = {"modified", "created"}
DUP_COLOR = QColor(220, 150, 40)      # duplicate clone badge
MISSING_COLOR = QColor(128, 128, 128)  # path gone
UPDATE_COLOR = QColor(134, 239, 235)   # release-update badge text
UPDATE_EDGE = QColor(86, 222, 219)     # glowing row border
UPDATE_BG_A = QColor(31, 58, 66)       # shaded row gradient, top-left
UPDATE_BG_B = QColor(48, 38, 66)       # shaded row gradient, bottom-right
PROJECT_VIEWS = {
    "all": ("All projects", "Your projects. Back on the radar."),
    "dirty": ("Working changes", "Pick up where you left off."),
    "git": ("Git repositories", "Every repository, in one place."),
    "ungitted": ("Loose projects", "Code that has not found a repository yet."),
    "duplicates": ("Duplicate clones", "One remote. More than one local home."),
    "missing": ("Missing paths", "Not found on the last scan. Kept for reference."),
}


def project_label(project: dict) -> str:
    name = project.get("name") or ""
    branch = project.get("branch")
    return f"{name}/{branch}" if name and branch else name


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _norm_version(tag: str) -> tuple:
    """Version-sortable key so Release Updated sorts by tag, not text."""
    import re
    parts = re.split(r"[.\-+]", (tag or "").lstrip("vV"))
    return tuple(int(p) if p.isdigit() else -1 for p in parts)


class ProjectsModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self._dup_remotes: set[str] = set()
        self._updates_by_path: dict[str, dict] = {}

    def refresh(self) -> None:
        conn = db.connect()
        self.beginResetModel()
        self._rows = [dict(r) for r in db.all_projects(conn)]
        self._dup_remotes = db.duplicate_remotes(conn)
        self._updates_by_path = {
            r["path"]: dict(r) for r in db.available_updates(conn) if r["path"]
        }
        self.endResetModel()
        conn.close()

    def update_for(self, proj: dict) -> dict | None:
        """The available-release row for a project, when a newer release exists."""
        return self._updates_by_path.get(proj["path"])

    def updates_count(self, exclude_repo: str = "") -> int:
        """Projects with a newer release available.

        `exclude_repo` (codeHOUND's own repo) is left out — its update goes
        through 'Update codeHOUND…', not the Install Updates button.
        """
        if not exclude_repo:
            return len(self._updates_by_path)
        return sum(1 for u in self._updates_by_path.values()
                   if u["repo"] != exclude_repo)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section][0]
        return None

    def row(self, r: int) -> dict:
        return self._rows[r]

    def matches_view(self, proj: dict, view: str) -> bool:
        if view == "all":
            return True
        if view == "missing":
            return bool(proj.get("missing"))
        if proj.get("missing"):
            return False
        if view == "dirty":
            return bool(proj.get("dirty"))
        if view == "git":
            return proj.get("kind") in ("git", "bare")
        if view == "ungitted":
            return proj.get("kind") == "ungitted"
        if view == "duplicates":
            return proj.get("remote_url") in self._dup_remotes
        raise ValueError(f"Unknown project view: {view}")

    def view_counts(self) -> dict[str, int]:
        return {
            view: sum(self.matches_view(row, view) for row in self._rows)
            for view in PROJECT_VIEWS
        }

    def featured_projects(self, pinned_paths: set[str]) -> list[dict]:
        live = sorted(
            (row for row in self._rows if not row.get("missing")),
            key=lambda row: (row.get("modified") or "", row["path"]),
            reverse=True,
        )
        featured = live[:5]
        recent_paths = {row["path"] for row in featured}
        return featured + [
            row for row in live
            if row["path"] in pinned_paths and row["path"] not in recent_paths
        ]

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        proj = self._rows[index.row()]
        field = COLUMNS[index.column()][1]
        update = self._updates_by_path.get(proj["path"])

        if role == Qt.ItemDataRole.DisplayRole:
            if field == "release":
                if not update:
                    return ""
                return f"⬆ {update['latest_tag']}" if update.get("latest_tag") else "⬆ new"
            val = proj.get(field)
            if field in DATE_FIELDS:
                return _fmt_date(val)
            if field == "dirty":
                return "▲ dirty" if val else ""
            if field == "name":
                marks = []
                if proj.get("dirty"):
                    marks.append("✱")
                if proj.get("adopted_to"):
                    marks.append("⌂")
                if proj.get("remote_url") in self._dup_remotes:
                    marks.append("⧉")
                if update:
                    marks.append("⬆")
                return f"{project_label(proj)} {' '.join(marks)}".rstrip()
            return val or ""
        if role == Qt.ItemDataRole.ToolTipRole:
            tips = [project_label(proj), proj["path"]]
            if proj.get("last_commit_msg"):
                tips.append(f"last commit: {proj['last_commit_msg']}")
            if proj.get("dirty"):
                tips.append("✱ uncommitted changes")
            if proj.get("adopted_to"):
                tips.append(f"⌂ adopted to {proj['adopted_to']}")
            if proj.get("remote_url") in self._dup_remotes:
                tips.append("⧉ same remote cloned elsewhere")
            if update:
                cur = update.get("current_tag") or "unknown"
                tips.append(f"⬆ release {update.get('latest_tag')} available (you have {cur})")
                if update.get("compatible"):
                    tips.append("   installable on this machine")
            if proj.get("missing"):
                tips.append("missing on last scan")
            return "\n".join(tips)
        if role == Qt.ItemDataRole.ForegroundRole:
            if proj.get("missing"):
                return QBrush(MISSING_COLOR)
            if field == "release" and update:
                return QBrush(UPDATE_COLOR)
            if field == "name" and proj.get("remote_url") in self._dup_remotes:
                return QBrush(DUP_COLOR)
        if role == Qt.ItemDataRole.BackgroundRole:
            if update and not proj.get("missing"):
                grad = QLinearGradient(0, 0, 1, 1)
                grad.setColorAt(0.0, UPDATE_BG_A)
                grad.setColorAt(1.0, UPDATE_BG_B)
                grad.setCoordinateMode(QLinearGradient.CoordinateMode.ObjectMode)
                return QBrush(grad)
        if role == PATH_ROLE:
            return proj["path"]
        if role == UPDATE_ROLE:
            return update
        if role == Qt.ItemDataRole.UserRole:  # raw value for sorting
            if field == "name":
                return project_label(proj)
            if field == "dirty":
                return 1 if proj.get("dirty") else 0
            if field == "release":
                return _norm_version(update.get("latest_tag") or "") if update else ()
            return proj.get(field) or ""
        return None


class UpdateRowDelegate(QStyledItemDelegate):
    """Draws a glowing gradient border around rows that have a release update.

    The shaded background comes from the model's BackgroundRole; this adds the
    highlighted edges so an updated app stands out at a glance.
    """

    def paint(self, painter, option, index):
        # proxy models forward custom roles to the source, so index.data works
        # whether the view shows the source or the proxy
        update = index.data(UPDATE_ROLE)
        if update is None:
            super().paint(painter, option, index)
            return
        super().paint(painter, option, index)
        rect = option.rect.adjusted(1, 1, -2, -2)
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        glow = QColor(UPDATE_EDGE)
        glow.setAlpha(70)
        pen = QPen(glow, 3.0)
        painter.setPen(pen)
        painter.drawRoundedRect(rect, 6, 6)
        pen = QPen(UPDATE_EDGE, 1.2)
        painter.setPen(pen)
        painter.drawRoundedRect(rect, 6, 6)
        painter.restore()


class ProjectsProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self._needle = ""
        self._view = "all"
        self._updates_only = False

    def set_view(self, view: str) -> None:
        if view not in PROJECT_VIEWS:
            raise ValueError(f"Unknown project view: {view}")
        self._view = view
        self.invalidateFilter()

    def set_updates_only(self, on: bool) -> None:
        self._updates_only = on
        self.invalidateFilter()

    def updates_only(self) -> bool:
        return self._updates_only

    def set_needle(self, text: str) -> None:
        self._needle = text.lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        proj = self.sourceModel().row(source_row)
        if not self.sourceModel().matches_view(proj, self._view):
            return False
        if self._updates_only and not self.sourceModel().update_for(proj):
            return False
        if not self._needle:
            return True
        hay = (project_label(proj) + " " + " ".join(
            str(proj.get(f) or "")
            for f in ("name", "path", "description", "language", "branch", "kind")
        )).lower()
        return all(word in hay for word in self._needle.split())
