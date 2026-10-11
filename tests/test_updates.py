"""Update-manager checks (phase 1: release detection).
Run with QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codehound import db
from codehound.updates import (
    WatchTarget,
    check_updates,
    local_tag,
    newer,
    parse_github_repo,
    pick_asset,
    watch_targets,
)


def _asset(name, url=None):
    return {"name": name, "browser_download_url": url or f"https://x/{name}"}


class ParseTests(unittest.TestCase):
    def test_https(self):
        self.assertEqual(parse_github_repo("https://github.com/zacpr/codeHOUND.git"),
                         "zacpr/codeHOUND")
        self.assertEqual(parse_github_repo("https://github.com/zacpr/codeHOUND"),
                         "zacpr/codeHOUND")
        self.assertEqual(parse_github_repo("https://github.com/zacpr/codeHOUND/"),
                         "zacpr/codeHOUND")

    def test_ssh(self):
        self.assertEqual(parse_github_repo("git@github.com:zacpr/appX.git"), "zacpr/appX")
        self.assertEqual(parse_github_repo("ssh://git@github.com/zacpr/appX.git"), "zacpr/appX")

    def test_non_github(self):
        for url in ("https://dev.azure.com/org/proj/_git/repo",
                    "https://gitlab.com/a/b", "", None, "/local/path",
                    "https://github.com/onlyowner"):
            self.assertIsNone(parse_github_repo(url), url)


class VersionTests(unittest.TestCase):
    def test_newer(self):
        self.assertTrue(newer("v1.10.0", "v1.9.2"))
        self.assertTrue(newer("1.2.0", "1.1.9"))
        self.assertFalse(newer("v1.0.0", "v1.0.0"))
        self.assertFalse(newer("", "v1.0.0"))
        self.assertTrue(newer("v2.0.0", ""))       # unknown local = flag it
        self.assertFalse(newer("v1.0.0", "v2.0.0"))

    def test_non_numeric_tags_conservative(self):
        # unparseable versions are never flagged as newer (no false positives)
        self.assertFalse(newer("beta2", "alpha1"))
        self.assertFalse(newer("same", "same"))
        self.assertTrue(newer("v1.0.0", "beta2"))   # numeric beats non-numeric


class AssetMatchTests(unittest.TestCase):
    def test_prefers_appimage_on_linux_x86_64(self):
        assets = [_asset("appX-1.2.0.deb"), _asset("appX-1.2.0-x86_64.AppImage"),
                  _asset("appX-1.2.0-win64.exe"), _asset("appX-1.2.0-aarch64.AppImage")]
        best, reason = pick_asset(assets, "Linux", "x86_64")
        self.assertIsNotNone(best)
        self.assertEqual(best["name"], "appX-1.2.0-x86_64.AppImage")
        self.assertEqual(reason, "")

    def test_rejects_foreign_arch(self):
        assets = [_asset("appX-1.2.0-aarch64.rpm"), _asset("appX-1.2.0-aarch64.tar.xz")]
        best, reason = pick_asset(assets, "Linux", "x86_64")
        self.assertIsNone(best)
        self.assertIn("x86_64", reason)

    def test_arch_agnostic_asset_accepted(self):
        best, reason = pick_asset([_asset("appX-1.2.0.tar.gz")], "Linux", "x86_64")
        self.assertEqual(best["name"], "appX-1.2.0.tar.gz")

    def test_skips_source_archives(self):
        best, reason = pick_asset([_asset("source.zip"), _asset("source.tar.gz")],
                                  "Linux", "x86_64")
        self.assertIsNone(best)
        self.assertIn("no asset matches", reason)

    def test_no_assets(self):
        best, reason = pick_asset([], "Linux", "x86_64")
        self.assertIsNone(best)
        self.assertEqual(reason, "release has no assets")

    def test_mac_needs_mac(self):
        assets = [_asset("appX-1.2.0-x86_64.AppImage"), _asset("appX-1.2.0.dmg")]
        best, _ = pick_asset(assets, "Darwin", "aarch64")
        self.assertEqual(best["name"], "appX-1.2.0.dmg")


class LocalTagTests(unittest.TestCase):
    def test_not_a_repo(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(local_tag(d), "")

    def test_describe_ahead_of_tag(self):
        # a fake git that reports being 3 commits past v1.0.0
        with patch("codehound.updates.run", return_value="v1.0.0-3-gabc1234\n"):
            self.assertEqual(local_tag("/anywhere"), "v1.0.0")
        with patch("codehound.updates.run", return_value="v2.0.0\n"):
            self.assertEqual(local_tag("/anywhere"), "v2.0.0")
        with patch("codehound.updates.run", return_value="abc1234\n"):
            self.assertEqual(local_tag("/anywhere"), "abc1234")


class DbRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch("codehound.db.DB_PATH", Path(self.temp.name) / "cat.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def test_updates_table_exists(self):
        self.conn.execute("SELECT repo, auto FROM updates LIMIT 0")

    def test_upsert_keeps_auto_flag(self):
        row = {"repo": "zacpr/appX", "path": "/r/appX", "name": "appX",
               "current_tag": "v1.0.0", "latest_tag": "v1.1.0",
               "published_at": "2026-10-01T00:00:00Z", "asset_name": "a.AppImage",
               "asset_url": "https://x/a.AppImage", "available": 1, "compatible": 1,
               "reason": "", "last_checked": db.now()}
        db.upsert_update(self.conn, row)
        db.set_update_auto(self.conn, "zacpr/appX", "auto")
        db.upsert_update(self.conn, {**row, "latest_tag": "v1.2.0"})
        got = db.get_update(self.conn, "zacpr/appX")
        self.assertEqual(got["latest_tag"], "v1.2.0")
        self.assertEqual(got["auto"], 1)          # survived the refresh

    def test_available_and_seen(self):
        base = {"path": "/r/x", "name": "x", "current_tag": "v1", "latest_tag": "v2",
                "published_at": None, "asset_name": None, "asset_url": None,
                "available": 1, "compatible": 0, "reason": "no asset matches",
                "last_checked": db.now()}
        db.upsert_update(self.conn, {**base, "repo": "o/a"})
        db.upsert_update(self.conn, {**base, "repo": "o/b", "available": 0})
        self.assertEqual([r["repo"] for r in db.available_updates(self.conn)], ["o/a"])
        db.mark_updates_seen(self.conn)
        self.assertEqual(db.get_update(self.conn, "o/a")["seen"], 1)


class WatchTargetsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch("codehound.db.DB_PATH", Path(self.temp.name) / "cat.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def _add(self, path, remote, missing=0):
        db.upsert_project(self.conn, {
            "path": path, "name": Path(path).name, "root": "/r", "kind": "git",
            "remote_url": remote, "language": None, "branch": None,
            "last_commit_date": None, "last_commit_msg": None, "dirty": 0,
            "created": None, "modified": None, "size_approx": None})
        if missing:
            self.conn.execute("UPDATE projects SET missing = 1 WHERE path = ?", (path,))
        self.conn.commit()

    def test_filters_github_live_unique(self):
        self._add("/r/appX", "https://github.com/zacpr/appX.git")
        self._add("/r/appX-wt", "https://github.com/zacpr/appX.git")   # dup remote
        self._add("/r/ado", "https://dev.azure.com/o/p/_git/repo")
        self._add("/r/gone", "https://github.com/zacpr/gone.git", missing=1)
        self._add("/r/plain", None)
        targets = watch_targets(self.conn)
        self.assertEqual([t.repo for t in targets], ["zacpr/appX"])
        self.assertEqual(targets[0].path, "/r/appX")


class _Cfg:
    accounts: dict = {}
    updates_enabled = True
    updates_mode = "stage"


class CheckUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        for target, value in (("codehound.db.DB_PATH", Path(self.temp.name) / "cat.db"),
                              ("codehound.updates.local_tag", lambda p: "v1.0.0")):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.upsert_project(self.conn, {
            "path": "/r/appX", "name": "appX", "root": "/r", "kind": "git",
            "remote_url": "https://github.com/zacpr/appX.git", "language": None,
            "branch": None, "last_commit_date": None, "last_commit_msg": None,
            "dirty": 0, "created": None, "modified": None, "size_approx": None})
        self.conn.commit()

    def test_available_update_recorded(self):
        releases = [{"tag_name": "v1.1.0", "draft": False, "prerelease": False,
                     "published_at": "2026-10-01T00:00:00Z",
                     "assets": [_asset("appX-v1.1.0-x86_64.AppImage")]},
                    {"tag_name": "v1.2.0-rc1", "draft": False, "prerelease": True,
                     "published_at": "2026-10-05T00:00:00Z", "assets": []}]
        with patch("codehound.updates.http_json", return_value=releases):
            available = check_updates(_Cfg(), self.conn, progress=lambda m: None)
        self.assertEqual([r["repo"] for r in available], ["zacpr/appX"])
        row = db.get_update(self.conn, "zacpr/appX")
        self.assertEqual(row["latest_tag"], "v1.1.0")     # prerelease skipped
        self.assertEqual(row["asset_name"], "appX-v1.1.0-x86_64.AppImage")
        self.assertEqual(row["compatible"], 1)

    def test_up_to_date_not_flagged(self):
        releases = [{"tag_name": "v1.0.0", "draft": False, "prerelease": False,
                     "published_at": "2026-01-01T00:00:00Z", "assets": []}]
        with patch("codehound.updates.http_json", return_value=releases):
            available = check_updates(_Cfg(), self.conn, progress=lambda m: None)
        self.assertEqual(available, [])
        self.assertEqual(db.get_update(self.conn, "zacpr/appX")["reason"], "up to date")

    def test_repo_failure_does_not_sink_check(self):
        from codehound.providers.base import SyncError

        def flaky(url, headers):
            raise SyncError("HTTP 404")
        with patch("codehound.updates.http_json", side_effect=flaky):
            available = check_updates(_Cfg(), self.conn, progress=lambda m: None)
        self.assertEqual(available, [])   # no rows written, no exception raised

    def test_no_releases_reason(self):
        with patch("codehound.updates.http_json", return_value=[]):
            check_updates(_Cfg(), self.conn, progress=lambda m: None)
        self.assertEqual(db.get_update(self.conn, "zacpr/appX")["reason"],
                         "no published releases")


if __name__ == "__main__":
    unittest.main()
