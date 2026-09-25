"""Isolated Qt checks. Run with QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests."""
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QProcess, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from codehound import config, db
from codehound.app import Dashboard, ICON_PATH, MASCOT_PATH
from codehound.install import install_icons
from codehound.model import PATH_ROLE, project_label
from codehound.theme import apply_theme


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        apply_theme(cls.app)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for target, value in (
            ("codehound.config.CONFIG_DIR", root),
            ("codehound.config.CONFIG_PATH", root / "config.toml"),
            ("codehound.db.DATA_DIR", root),
            ("codehound.db.DB_PATH", root / "catalogue.db"),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        cfg = config.load_config()
        cfg.ai_provider = "custom"
        cfg.ai_base_url = "http://localhost:1234/v1"
        cfg.ai_model = "test-model"
        cfg.launchers = {"editor": "editor {path}", "terminal": "terminal {path}"}
        config.save_config(cfg)
        conn = db.connect()
        try:
            for i in range(8):
                db.upsert_project(conn, {
                    "path": f"/projects/project-{i}", "name": f"Project {i}",
                    "root": "/projects", "kind": "ungitted" if i == 6 else "git",
                    "language": "Python", "branch": "main",
                    "modified": f"2026-09-{25-i:02d}T12:00:00+00:00",
                    "dirty": int(i in (0, 7)),
                    "remote_url": "https://example.org/repo" if i in (0, 1) else None,
                    "last_commit_msg": f"Implement feature {i}",
                    "last_commit_date": f"2026-09-{25-i:02d}T12:00:00+00:00",
                })
            conn.execute("UPDATE projects SET missing = 1 WHERE path = '/projects/project-7'")
            conn.execute("UPDATE projects SET description = 'A saved AI description.'")
            conn.commit()
        finally:
            conn.close()
        self.win = Dashboard()
        self.win.show()
        self.app.processEvents()
        self.addCleanup(self.dispose_window)

    def dispose_window(self):
        self.win.hide()
        self.win.deleteLater()
        self.app.processEvents()

    def select(self, path):
        for row in range(self.win.proxy.rowCount()):
            if self.win.proxy.index(row, 0).data(PATH_ROLE) == path:
                self.win.table.selectRow(row)
                self.app.processEvents()
                return
        self.fail(f"Not found: {path}")

    def test_views_search_counts_and_empty_state(self):
        self.assertEqual(self.win.model.view_counts(), {
            "all": 8, "dirty": 1, "git": 6, "ungitted": 1, "duplicates": 2, "missing": 1,
        })
        QTest.mouseClick(self.win.nav_buttons["dirty"], Qt.MouseButton.LeftButton)
        self.assertEqual(self.win.proxy.rowCount(), 1)
        self.win.search.setText("project 0 python")
        self.assertEqual(self.win.proxy.rowCount(), 1)
        self.win.search.setText("not-present")
        self.assertEqual(self.win.proxy.rowCount(), 0)
        self.assertEqual(self.win.results.currentIndex(), 1)
        self.assertFalse(self.win.open_btn.isEnabled())
        self.win._reset_filters()
        self.assertEqual(self.win.proxy.rowCount(), 8)
        self.assertEqual(self.win.results.currentIndex(), 0)
        with self.assertRaises(ValueError):
            self.win.proxy.set_view("unknown")

    def test_inspector_actions_and_selection_survive_sort_and_reload(self):
        self.select("/projects/project-1")
        self.assertEqual(self.win.detail_title.text(), "Project 1/main")
        self.win.table.sortByColumn(0, Qt.SortOrder.DescendingOrder)
        with patch("codehound.app.launchers.launch") as launch:
            self.win.launcher_combo.setCurrentText("terminal")
            self.win.open_btn.click()
            launch.assert_called_once_with(self.win.cfg, "terminal", "/projects/project-1")
        with patch.object(self.win, "_ask_custom") as ask:
            self.win.project_ask_btn.click()
            ask.assert_called_once_with("/projects/project-1")
        self.win.copy_path_btn.click()
        self.assertEqual(self.app.clipboard().text(), "/projects/project-1")
        self.win.reload()
        self.assertEqual(self.win._selected_path(), "/projects/project-1")
        self.assertEqual(self.win.launcher_combo.currentText(), "terminal")
        self.win.search.setText("no-match")
        self.assertFalse(self.win.copy_path_btn.isEnabled())
        self.assertFalse(self.win.project_ask_btn.isEnabled())

    def test_recent_five_extra_pins_and_persistence(self):
        self.assertEqual([p["path"] for p in self.win._featured],
                         [f"/projects/project-{i}" for i in range(5)])
        self.select("/projects/project-6")
        self.win.pin_btn.click()
        self.assertEqual(len(self.win._featured), 6)
        self.assertEqual(self.win._featured[-1]["path"], "/projects/project-6")
        self.assertEqual(self.win._load_pins(), {"/projects/project-6"})
        self.win._step_featured(-1)
        self.assertEqual(self.win.feature_position.text(), "6 / 6")
        self.assertEqual(self.win.feature_name.text(), "Project 6/main")
        self.assertIn("saved AI", self.win.feature_summary.text())
        self.assertIn("Implement feature 6", self.win.feature_previous.text())
        self.win.feature_pin.click()
        self.assertEqual(len(self.win._featured), 5)
        self.assertEqual(self.win._load_pins(), set())
        self.win.feature_pin.click()
        self.assertEqual(len(self.win._featured), 5)  # Pinning a recent project does not duplicate it.
        self.win._toggle_pin("/projects/project-7")
        self.assertEqual(len(self.win._featured), 5)  # Missing paths are not featured.
        self.assertIn("/projects/project-7", self.win._load_pins())

    def test_feature_actions_and_ai_gating(self):
        with patch.object(self.win, "_ask_about") as ask:
            self.win.feature_brief.click()
            self.assertEqual(ask.call_args.args[0], "/projects/project-0")
            self.assertIn("Separate implemented work from plans", ask.call_args.args[1])
        self.win._set_view("ungitted")
        self.win.search.setText("no-match")
        self.win.feature_inspect.click()
        self.assertEqual(self.win._selected_path(), "/projects/project-0")
        self.assertEqual(self.win.search.text(), "")
        cfg = config.load_config()
        cfg.ai_model = ""
        config.save_config(cfg)
        self.win.reload()
        self.assertFalse(self.win.feature_brief.isEnabled())
        self.assertFalse(self.win.project_ask_btn.isEnabled())
        self.win._set_view("missing")
        self.select("/projects/project-7")
        self.assertFalse(self.win.open_btn.isEnabled())
        self.assertTrue(self.win.copy_path_btn.isEnabled())

    def test_empty_catalogue_and_bounded_carousel(self):
        conn = db.connect()
        try:
            conn.execute("DELETE FROM projects")
            conn.commit()
        finally:
            conn.close()
        self.win.reload()
        self.assertEqual(self.win._featured, [])
        self.assertEqual(self.win.feature_position.text(), "0 / 0")
        self.assertFalse(self.win.feature_next.isEnabled())
        self.assertFalse(self.win.feature_brief.isEnabled())
        self.assertEqual(self.win.results.currentIndex(), 1)
        self.assertIn("radar", self.win.empty_title.text())

    def test_asset_compact_layout_and_column_order(self):
        self.assertFalse(QPixmap(str(MASCOT_PATH)).isNull())
        self.assertFalse(QPixmap(str(ICON_PATH)).isNull())
        self.win.resize(1060, 740)
        self.app.processEvents()
        self.assertEqual(self.win.width(), 1060)
        self.assertEqual(self.win.height(), 740)
        self.assertTrue(self.win.metrics_panel.isHidden())
        self.assertGreaterEqual(self.win.table.height(), 150)
        self.assertTrue(self.win.inspector.rect().contains(
            self.win.open_btn.mapTo(self.win.inspector, self.win.open_btn.rect().center())
        ))
        self.win.details_btn.click()
        self.assertTrue(self.win.inspector.isHidden())
        height = self.win.table.height()
        self.win.feature_toggle.click()
        self.app.processEvents()
        self.assertGreater(self.win.table.height(), height)
        hdr = self.win.table.horizontalHeader()
        hdr.moveSection(hdr.visualIndex(9), 1)
        conn = db.connect()
        try:
            self.assertEqual(db.kv_get(conn, "column_order").split(",")[1], "9")
        finally:
            conn.close()
        self.assertTrue(self.win.table.isColumnHidden(8))
        self.win.resize(1440, 860)
        self.app.processEvents()
        self.assertFalse(self.win.metrics_panel.isHidden())

    def test_cody_icon_installation_replaces_legacy_icon(self):
        root = Path(self.temp.name) / "icons"
        old = root / "scalable/apps/codehound.svg"
        old.parent.mkdir(parents=True)
        old.write_text("old icon")
        unrelated = old.with_name("another-app.svg")
        unrelated.write_text("keep")
        install_icons(root)
        self.assertFalse(old.exists())
        self.assertEqual(unrelated.read_text(), "keep")
        for size in (16, 22, 24, 32, 48, 64, 128, 256):
            image = QPixmap(str(root / f"{size}x{size}/apps/codehound-cody.png"))
            self.assertEqual((image.width(), image.height()), (size, size))
        install_icons(root)

    def test_background_task_controls_and_activity(self):
        with patch.object(QProcess, "start") as start:
            self.win.scan_btn.click()
            start.assert_called_once_with(sys.executable, ["-m", "codehound.cli", "scan"])
            self.assertFalse(self.win.scan_btn.isEnabled())
            self.win.start_scan()
            self.assertEqual(start.call_count, 1)
            self.win._task_output("scanning /projects")
            self.win._task_finished("_scan_proc", 0, self.win.scan_btn, "Scan Now", "scan")
            self.assertTrue(self.win.scan_btn.isEnabled())
            self.assertIn("scanning /projects", self.win.activity.toPlainText())
            self.win.start_sync()
            start.assert_called_with(sys.executable, ["-m", "codehound.cli", "sync"])
            proc = self.win._sync_proc
            self.win._task_start_error(QProcess.ProcessError.FailedToStart, proc,
                                       "_sync_proc", self.win.sync_btn, "Sync Accounts", "sync")
            self.assertTrue(self.win.sync_btn.isEnabled())
            self.assertIsNone(self.win._sync_proc)
            self.assertIn("could not start", self.win.activity.toPlainText())
            self.assertFalse(self.win.activity_dock.isHidden())

    def test_project_branch_labels_and_search(self):
        for project, expected in (
            ({"name": "repo", "branch": "main"}, "repo/main"),
            ({"name": "repo", "branch": "feature/new-ui"}, "repo/feature/new-ui"),
            ({"name": "repo", "branch": None}, "repo"),
            ({"name": "repo", "branch": ""}, "repo"),
            ({"name": "", "branch": "main"}, ""),
        ):
            self.assertEqual(project_label(project), expected)
        self.win.search.setText("Project 0/main")
        self.assertEqual(self.win.proxy.rowCount(), 1)
        index = self.win.proxy.index(0, 0)
        self.assertTrue(index.data().startswith("Project 0/main"))
        self.assertIn("✱", index.data())
        self.assertIn("⧉", index.data())
        self.assertEqual(index.data(Qt.ItemDataRole.UserRole), "Project 0/main")
        self.assertIn("Project 0/main", index.data(Qt.ItemDataRole.ToolTipRole))
        self.assertEqual(index.data(PATH_ROLE), "/projects/project-0")
        self.win.table.selectRow(0)
        self.assertEqual(self.win.detail_title.text(), "Project 0/main")
        self.assertEqual(self.win.feature_name.text(), "Project 0/main")


if __name__ == "__main__":
    unittest.main()
