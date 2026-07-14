"""Qt table model over the projects database."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    Qt,
)
from PySide6.QtGui import QBrush, QColor

from . import db

COLUMNS = [
    ("Name", "name"),
    ("Kind", "kind"),
    ("Language", "language"),
    ("Branch", "branch"),
    ("Modified", "modified"),
    ("Created", "created"),
    ("Root", "root"),
    ("Description", "description"),
    ("Path", "path"),
]
PATH_ROLE = Qt.ItemDataRole.UserRole + 1
DATE_FIELDS = {"modified", "created"}
DUP_COLOR = QColor(220, 150, 40)      # duplicate clone badge
MISSING_COLOR = QColor(128, 128, 128)  # path gone


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


class ProjectsModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []
        self._dup_remotes: set[str] = set()

    def refresh(self) -> None:
        conn = db.connect()
        self.beginResetModel()
        self._rows = [dict(r) for r in db.all_projects(conn)]
        self._dup_remotes = db.duplicate_remotes(conn)
        self.endResetModel()
        conn.close()

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

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        proj = self._rows[index.row()]
        field = COLUMNS[index.column()][1]

        if role == Qt.ItemDataRole.DisplayRole:
            val = proj.get(field)
            if field in DATE_FIELDS:
                return _fmt_date(val)
            if field == "name":
                marks = []
                if proj.get("dirty"):
                    marks.append("✱")
                if proj.get("adopted_to"):
                    marks.append("⌂")
                if proj.get("remote_url") in self._dup_remotes:
                    marks.append("⧉")
                return f"{val} {' '.join(marks)}".rstrip()
            return val or ""
        if role == Qt.ItemDataRole.ToolTipRole:
            tips = [proj["path"]]
            if proj.get("last_commit_msg"):
                tips.append(f"last commit: {proj['last_commit_msg']}")
            if proj.get("dirty"):
                tips.append("✱ uncommitted changes")
            if proj.get("adopted_to"):
                tips.append(f"⌂ adopted to {proj['adopted_to']}")
            if proj.get("remote_url") in self._dup_remotes:
                tips.append("⧉ same remote cloned elsewhere")
            if proj.get("missing"):
                tips.append("missing on last scan")
            return "\n".join(tips)
        if role == Qt.ItemDataRole.ForegroundRole:
            if proj.get("missing"):
                return QBrush(MISSING_COLOR)
            if field == "name" and proj.get("remote_url") in self._dup_remotes:
                return QBrush(DUP_COLOR)
        if role == PATH_ROLE:
            return proj["path"]
        if role == Qt.ItemDataRole.UserRole:  # raw value for sorting
            return proj.get(field) or ""
        return None


class ProjectsProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self._needle = ""

    def set_needle(self, text: str) -> None:
        self._needle = text.lower()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        if not self._needle:
            return True
        proj = self.sourceModel().row(source_row)
        hay = " ".join(
            str(proj.get(f) or "")
            for f in ("name", "path", "description", "language", "branch", "kind")
        ).lower()
        return all(word in hay for word in self._needle.split())
