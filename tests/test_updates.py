"""Update-manager checks (phase 1: release detection).
Run with QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codehound import db
from codehound.updates import (
    WatchTarget,
    _asset_href,
    _asset_href_for,
    _auth_headers,
    _download_verify,
    _gh_token,
    check_updates,
    local_tag,
    newer,
    parse_github_repo,
    pick_asset,
    published_sha256,
    watch_targets,
)
from codehound.providers.base import SyncError


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
    updates_own_only = False
    updates_install_dir = ""


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


class OwnOnlyWatchTests(unittest.TestCase):
    """own_only watches repos the authed user owns — not orgs, not forks."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch("codehound.db.DB_PATH", Path(self.temp.name) / "cat.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        for repo in ("zacpr/mine", "zacpr/old-fork", "someorg/theirs"):
            db.upsert_project(self.conn, {
                "path": f"/r/{repo.split('/')[1]}", "name": repo, "root": "/r",
                "kind": "git", "remote_url": f"https://github.com/{repo}.git",
                "language": None, "branch": None, "last_commit_date": None,
                "last_commit_msg": None, "dirty": 0, "created": None,
                "modified": None, "size_approx": None})
        self.conn.commit()

    def _api(self, cfg, path):
        if path == "user":
            return {"login": "zacpr"}
        if path.endswith("/releases?per_page=10"):
            return [{"tag_name": "v9.9.9", "draft": False, "prerelease": False,
                     "published_at": "2026-10-01T00:00:00Z",
                     "assets": [_asset("mine-v9.9.9-x86_64.AppImage")]}]
        if path in ("repos/zacpr/mine", "repos/zacpr/old-fork"):
            return {"fork": path.endswith("old-fork")}
        raise AssertionError(f"unexpected API call: {path}")

    def test_own_only_keeps_only_own_non_forks(self):
        cfg = _Cfg()
        cfg.updates_own_only = True
        with patch("codehound.updates._api_get", side_effect=self._api), \
                patch("codehound.updates.local_tag", lambda p: "v1.0.0"):
            check_updates(cfg, self.conn, progress=lambda m: None)
        watched = {r["repo"] for r in
                   self.conn.execute("SELECT repo FROM updates").fetchall()}
        self.assertEqual(watched, {"zacpr/mine"})

    def test_own_only_off_watches_everything(self):
        with patch("codehound.updates._api_get", side_effect=self._api), \
                patch("codehound.updates.local_tag", lambda p: "v1.0.0"):
            check_updates(_Cfg(), self.conn, progress=lambda m: None)
        watched = {r["repo"] for r in
                   self.conn.execute("SELECT repo FROM updates").fetchall()}
        self.assertEqual(watched, {"zacpr/mine", "zacpr/old-fork", "someorg/theirs"})

    def test_unauthenticated_falls_back_to_all(self):
        from codehound.providers.base import SyncError

        def deny(cfg, path):
            if path == "user":
                raise SyncError("HTTP 401")
            return self._api(cfg, path)
        cfg = _Cfg()
        cfg.updates_own_only = True
        with patch("codehound.updates._api_get", side_effect=deny), \
                patch("codehound.updates.local_tag", lambda p: "v1.0.0"):
            check_updates(cfg, self.conn, progress=lambda m: None)
        watched = {r["repo"] for r in
                   self.conn.execute("SELECT repo FROM updates").fetchall()}
        self.assertEqual(len(watched), 3)

    def test_own_only_prunes_stale_rows_from_earlier_checks(self):
        # A row left over from a run before own_only existed (or after the
        # repo was un-catalogued) must not linger in the updates table.
        db.upsert_update(self.conn, {
            "repo": "Niko1221/Strata", "path": "/r/strata", "name": "Strata",
            "current_tag": "v1.0.0", "latest_tag": "v2.0.0",
            "published_at": "2026-10-01T00:00:00Z", "asset_name": "s.AppImage",
            "asset_url": "https://example.org/s.AppImage", "available": 1,
            "compatible": 1, "reason": "", "last_checked": "2026-10-01T00:00:00Z"})
        self.conn.commit()
        cfg = _Cfg()
        cfg.updates_own_only = True
        with patch("codehound.updates._api_get", side_effect=self._api), \
                patch("codehound.updates.local_tag", lambda p: "v1.0.0"):
            check_updates(cfg, self.conn, progress=lambda m: None)
        watched = {r["repo"] for r in
                   self.conn.execute("SELECT repo FROM updates").fetchall()}
        self.assertEqual(watched, {"zacpr/mine"})
        self.assertEqual(
            [r["repo"] for r in db.available_updates(self.conn)], ["zacpr/mine"])

    def test_self_update_row_survives_pruning(self):
        cfg = _Cfg()
        cfg.updates_own_only = True
        cfg.self_repo = "zacpr/codeHOUND"
        db.upsert_update(self.conn, {
            "repo": "zacpr/codeHOUND", "path": None, "name": "codeHOUND",
            "current_tag": "v0.5.1.0", "latest_tag": "v0.5.2.0",
            "published_at": "2026-10-01T00:00:00Z", "asset_name": "c.rpm",
            "asset_url": "https://example.org/c.rpm", "available": 1,
            "compatible": 1, "reason": "", "last_checked": "2026-10-01T00:00:00Z"})
        self.conn.commit()
        with patch("codehound.updates._api_get", side_effect=self._api), \
                patch("codehound.updates.local_tag", lambda p: "v1.0.0"):
            check_updates(cfg, self.conn, progress=lambda m: None)
        watched = {r["repo"] for r in
                   self.conn.execute("SELECT repo FROM updates").fetchall()}
        self.assertEqual(watched, {"zacpr/mine", "zacpr/codeHOUND"})


# ---------------------------------------------------------------- phase 2

import hashlib
import os
import tarfile
import zipfile

from codehound.updates import (
    extract_archive,
    find_executable,
    install_update,
    published_sha256,
    rollback_update,
    sha256_of,
    slug,
)


def _make_exec(path: Path, content: bytes = b"#!/bin/sh\necho hi\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)
    return path


class ExtractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_targz(self):
        src = self.root / "build"
        _make_exec(src / "appX-1.0" / "appX")
        (src / "appX-1.0" / "README").write_text("hi")
        arc = self.root / "appX.tar.gz"
        with tarfile.open(arc, "w:gz") as tf:
            tf.add(src / "appX-1.0", arcname="appX-1.0")
        out = self.root / "out"
        extract_archive(arc, out)
        self.assertTrue(os.access(out / "appX-1.0" / "appX", os.X_OK))

    def test_zip_preserves_exec_bit(self):
        arc = self.root / "appX.zip"
        with zipfile.ZipFile(arc, "w") as zf:
            info = zipfile.ZipInfo("appX")
            info.external_attr = 0o755 << 16
            zf.writestr(info, "#!/bin/sh\n")
        out = self.root / "out"
        extract_archive(arc, out)
        self.assertTrue(os.access(out / "appX", os.X_OK))

    def test_zip_rejects_path_escape(self):
        arc = self.root / "evil.zip"
        with zipfile.ZipFile(arc, "w") as zf:
            zf.writestr("../escaped", "x")
        with self.assertRaises(Exception):
            extract_archive(arc, self.root / "out")

    def test_appimage_copied_executable(self):
        arc = self.root / "appX.AppImage"
        arc.write_bytes(b"ELF-ish")
        out = self.root / "out"
        extract_archive(arc, out)
        got = out / "appX.AppImage"
        self.assertTrue(got.is_file() and os.access(got, os.X_OK))


class FindExecutableTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_exact_name_match(self):
        _make_exec(self.root / "appX-1.0" / "appX")
        _make_exec(self.root / "appX-1.0" / "helper")
        self.assertEqual(find_executable(self.root, "appX").name, "appX")

    def test_single_top_level(self):
        _make_exec(self.root / "weirdname")
        self.assertEqual(find_executable(self.root, "appX").name, "weirdname")

    def test_apprun(self):
        _make_exec(self.root / "squashfs-root" / "AppRun")
        _make_exec(self.root / "squashfs-root" / "usr" / "lib")
        self.assertEqual(find_executable(self.root, "appX").name, "AppRun")

    def test_ambiguous_returns_none(self):
        _make_exec(self.root / "a" / "one")
        _make_exec(self.root / "b" / "two")
        self.assertIsNone(find_executable(self.root, "zzz"))


class PrivateAssetTests(unittest.TestCase):
    """A private repo's browser_download_url 404s; assets must come from the
    API asset endpoint with the same auth the API calls use."""

    def test_href_prefers_api_url(self):
        a = {"name": "x.rpm", "url": "https://api.github.com/releases/assets/1",
             "browser_download_url": "https://github.com/x/y/releases/download/v1/x.rpm"}
        self.assertEqual(_asset_href(a), "https://api.github.com/releases/assets/1")

    def test_href_falls_back_to_browser_url(self):
        a = {"name": "x.rpm", "browser_download_url": "https://github.com/x.rpm"}
        self.assertEqual(_asset_href(a), "https://github.com/x.rpm")

    def test_href_for_matches_by_name(self):
        rel = {"assets": [{"name": "a.rpm", "url": "https://api/a"},
                          {"name": "b.rpm", "url": "https://api/b"}]}
        self.assertEqual(_asset_href_for(rel, "b.rpm"), "https://api/b")
        self.assertEqual(_asset_href_for(rel, "missing.rpm"), "")
        self.assertEqual(_asset_href_for(None, "a.rpm"), "")

    def test_auth_ladder_pat_then_gh_then_anon(self):
        class Pat:
            token = "pat-token"
        cfg = type("C", (), {"accounts": {"github": [Pat()]}})
        self.assertEqual(_auth_headers(cfg), {"Authorization": "Bearer pat-token"})
        no_pat = type("C", (), {"accounts": {"github": [type("A", (), {"token": ""})]}})
        with patch("codehound.updates._gh_token", return_value="gh-token"):
            self.assertEqual(_auth_headers(no_pat), {"Authorization": "Bearer gh-token"})
        with patch("codehound.updates._gh_token", return_value=""):
            self.assertEqual(_auth_headers(no_pat), {})

    def test_gh_token_swallows_failures(self):
        with patch("codehound.updates.shutil.which", return_value="/usr/bin/gh"), \
             patch("codehound.updates.run", side_effect=SyncError("nope")):
            self.assertEqual(_gh_token(), "")
        with patch("codehound.updates.shutil.which", return_value=None):
            self.assertEqual(_gh_token(), "")

    def test_download_uses_api_href_and_auth(self):
        """The row's browser URL is not trusted when the release has an API href."""
        seen = {}

        def fake_download(url, dest, headers=None):
            seen["url"] = url
            seen["headers"] = headers or {}
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"payload")
            return dest
        release = {"assets": [{"name": "appX.rpm", "url": "https://api/assets/9",
                               "browser_download_url": "https://github.com/x/appX.rpm"}],
                   "body": ""}
        with patch("codehound.updates.download_asset", side_effect=fake_download), \
             patch("codehound.updates._release_by_tag", return_value=release), \
             patch("codehound.updates._auth_headers", return_value={"Authorization": "Bearer t"}), \
             patch("codehound.updates.downloads_dir", return_value=Path(tempfile.mkdtemp())):
            row = {"repo": "zacpr/appX", "asset_name": "appX.rpm",
                   "asset_url": "https://github.com/x/appX.rpm", "latest_tag": "v1"}
            _download_verify(type("C", (), {"accounts": {}}), row, lambda m: None)
        self.assertEqual(seen["url"], "https://api/assets/9")
        self.assertEqual(seen["headers"].get("Authorization"), "Bearer t")

    def test_checksum_fetch_uses_api_href(self):
        sums = "ab" * 32 + "  appX.rpm\n"
        captured = {}

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return sums.encode()

        def fake_urlopen(req, timeout=60):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            return Resp()
        release = {"assets": [{"name": "SHA256SUMS.txt",
                               "url": "https://api/assets/7",
                               "browser_download_url": "https://github.com/x/SHA256SUMS.txt"}],
                   "body": ""}
        with patch("codehound.updates.urlopen", side_effect=fake_urlopen):
            got = published_sha256(release, "appX.rpm", {"Authorization": "Bearer t"})
        self.assertEqual(got, "ab" * 32)
        self.assertEqual(captured["url"], "https://api/assets/7")
        self.assertEqual(captured["auth"], "Bearer t")


class ChecksumTests(unittest.TestCase):
    def test_body_hash_found(self):
        h = "ab" * 32
        release = {"assets": [], "body": f"notes\n{h}  appX.AppImage\n"}
        self.assertEqual(published_sha256(release, "appX.AppImage"), h)

    def test_no_hash_returns_empty(self):
        self.assertEqual(published_sha256({"assets": [], "body": ""}, "x"), "")

    def test_sha256_of(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "f"
            p.write_bytes(b"hello")
            self.assertEqual(sha256_of(p), hashlib.sha256(b"hello").hexdigest())


class InstallTests(unittest.TestCase):
    """End-to-end install/rollback with the network download patched out."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

        def _user_bin():
            d = self.root / "bin"
            d.mkdir(parents=True, exist_ok=True)
            return d

        for target, value in (
            ("codehound.db.DB_PATH", self.root / "cat.db"),
            ("codehound.updates.DATA_DIR", self.root / "data"),
            ("codehound.updates.user_bin_dir", _user_bin),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        # a release row ready to install (as check_updates would leave it)
        db.upsert_update(self.conn, {
            "repo": "zacpr/appX", "path": "/r/appX", "name": "appX",
            "current_tag": "v1.0.0", "latest_tag": "v1.1.0",
            "published_at": None, "asset_name": "appX-v1.1.0.tar.gz",
            "asset_url": "https://x/appX.tar.gz", "available": 1, "compatible": 1,
            "reason": "", "last_checked": db.now()})
        self.conn.commit()
        # build the archive the "download" will produce
        src = self.root / "build" / "appX-1.1.0"
        _make_exec(src / "appX", b"#!/bin/sh\necho v1.1.0\n")
        self.archive = self.root / "appX.tar.gz"
        with tarfile.open(self.archive, "w:gz") as tf:
            tf.add(src, arcname="appX-1.1.0")

        def fake_download(url, dest, headers=None):
            import shutil as sh
            dest.parent.mkdir(parents=True, exist_ok=True)
            sh.copy2(self.archive, dest)
            return dest
        patcher = patch("codehound.updates.download_asset", side_effect=fake_download)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("codehound.updates._release_by_tag",
                        return_value={"assets": [], "body": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.noop = lambda m: None

    def _row(self):
        return db.get_update(self.conn, "zacpr/appX")

    def test_managed_install_and_symlink(self):
        summary = install_update(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        row = self._row()
        self.assertEqual(row["install_mode"], "managed")
        self.assertEqual(row["installed_tag"], "v1.1.0")
        link = self.root / "bin" / "appx"
        self.assertTrue(link.is_symlink())
        self.assertIn("v1.1.0", str(link.resolve()))
        self.assertIn("installed", summary)
        self.assertEqual(row["available"], 0)

    def test_second_install_keeps_prev_for_rollback(self):
        install_update(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        # bump to v1.2.0 and install again
        _make_exec(self.root / "build2" / "appX", b"#!/bin/sh\necho v1.2.0\n")
        with tarfile.open(self.archive, "w:gz") as tf:
            tf.add(self.root / "build2" / "appX", arcname="appX")
        self.conn.execute("UPDATE updates SET latest_tag='v1.2.0', available=1 "
                          "WHERE repo='zacpr/appX'")
        self.conn.commit()
        install_update(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        row = self._row()
        self.assertEqual(row["installed_tag"], "v1.2.0")
        self.assertEqual(row["prev_tag"], "v1.1.0")
        self.assertTrue(Path(row["prev_dir"]).is_dir())
        # rollback repoints the symlink at the old version
        rollback_update(self.conn, "zacpr/appX", progress=self.noop)
        row = self._row()
        self.assertEqual(row["installed_tag"], "v1.1.0")
        self.assertIn("v1.1.0", str((self.root / "bin" / "appx").resolve()))

    def test_inplace_replace_with_backup(self):
        existing = self.root / "appX"
        _make_exec(existing, b"old binary")
        self.conn.execute("UPDATE updates SET install_exe=? WHERE repo='zacpr/appX'",
                          (str(existing),))
        self.conn.commit()
        install_update(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        row = self._row()
        self.assertEqual(row["install_mode"], "inplace")
        self.assertEqual(existing.read_bytes(), b"#!/bin/sh\necho v1.1.0\n")
        backup = existing.with_suffix(".codehound-bak")
        self.assertTrue(backup.is_file())
        # rollback restores the original bytes
        rollback_update(self.conn, "zacpr/appX", progress=self.noop)
        self.assertEqual(existing.read_bytes(), b"old binary")

    def test_custom_path_override(self):
        custom = self.root / "opt" / "appdir"
        custom.mkdir(parents=True)
        db.set_update_install_path(self.conn, "zacpr/appX", str(custom))
        self.conn.commit()
        install_update(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        row = self._row()
        self.assertEqual(row["install_mode"], "custom")
        self.assertTrue((custom / "appX").is_file())

    def test_sha_mismatch_refuses_install(self):
        good = sha256_of(self.archive)
        with patch("codehound.updates._release_by_tag",
                   return_value={"assets": [], "body": f"dead{good[4:]}  appX-v1.1.0.tar.gz"}):
            with self.assertRaises(Exception) as ctx:
                install_update(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        self.assertIn("SHA-256 mismatch", str(ctx.exception))
        self.assertEqual(self._row()["available"], 1)   # row untouched

    def test_sha_match_installs(self):
        good = sha256_of(self.archive)
        with patch("codehound.updates._release_by_tag",
                   return_value={"assets": [], "body": f"{good}  appX-v1.1.0.tar.gz"}):
            install_update(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        self.assertEqual(self._row()["install_mode"], "managed")

    def test_not_on_watch_list(self):
        from codehound.providers.base import SyncError
        with self.assertRaises(SyncError):
            install_update(_Cfg(), self.conn, "zacpr/nope", progress=self.noop)

    def test_unsupported_asset_format(self):
        from codehound.providers.base import SyncError
        self.conn.execute("UPDATE updates SET asset_name='appX.msi', "
                          "asset_url='https://x/appX.msi' WHERE repo='zacpr/appX'")
        self.conn.commit()
        with self.assertRaises(SyncError):
            install_update(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)

    def _cwd_sentinel(self) -> Path:
        """chdir into a dir holding a sentinel file — an empty staged_dir once
        became Path("") == cwd and rmtree'd the whole checkout."""
        cwd = self.root / "cwd"
        cwd.mkdir()
        sentinel = cwd / "SENTINEL"
        sentinel.write_text("keep me")
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(cwd)
        return sentinel

    def test_install_never_staged_leaves_cwd_alone(self):
        sentinel = self._cwd_sentinel()
        self.assertIsNone(self._row()["staged_dir"])
        install_update(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        self.assertTrue(sentinel.is_file())

    def test_stage_with_empty_staged_dir_leaves_cwd_alone(self):
        from codehound.updates import stage_update
        sentinel = self._cwd_sentinel()
        self.conn.execute("UPDATE updates SET staged_tag='v1.0.0', staged_dir='' "
                          "WHERE repo='zacpr/appX'")
        self.conn.commit()
        stage_update(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        self.assertTrue(sentinel.is_file())

    def test_activate_with_empty_staged_dir_refuses(self):
        from codehound.providers.base import SyncError
        from codehound.updates import activate_staged
        sentinel = self._cwd_sentinel()
        self.conn.execute("UPDATE updates SET staged_tag='v1.1.0', staged_dir='' "
                          "WHERE repo='zacpr/appX'")
        self.conn.commit()
        with self.assertRaises(SyncError):
            activate_staged(self.conn, "zacpr/appX", progress=self.noop)
        self.assertTrue(sentinel.is_file())

    def test_staged_dir_outside_downloads_is_not_deleted(self):
        outside = self.root / "not-a-stage"
        outside.mkdir()
        (outside / "file").write_text("x")
        self.conn.execute("UPDATE updates SET staged_tag='v1.0.0', staged_dir=? "
                          "WHERE repo='zacpr/appX'", (str(outside),))
        self.conn.commit()
        install_update(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        self.assertTrue((outside / "file").is_file())
        self.assertFalse(self._row()["staged_dir"])

    def test_rollback_with_empty_paths_refuses(self):
        from codehound.providers.base import SyncError
        sentinel = self._cwd_sentinel()
        for mode, col in (("managed", "prev_dir"), ("inplace", "install_exe")):
            self.conn.execute(f"UPDATE updates SET install_mode=?, {col}='' "
                              "WHERE repo='zacpr/appX'", (mode,))
            self.conn.commit()
            with self.assertRaises(SyncError):
                rollback_update(self.conn, "zacpr/appX", progress=self.noop)
        self.assertTrue(sentinel.is_file())


class CliInstallAllTests(unittest.TestCase):
    """Regression: 'codehound updates --install-all' crashed with
    AttributeError because the updates subparser never defined --exe."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch("codehound.db.DB_PATH", Path(self.temp.name) / "cat.db")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        db.upsert_update(self.conn, {
            "repo": "zacpr/appX", "path": "/r/appX", "name": "appX",
            "current_tag": "v1.0.0", "latest_tag": "v1.1.0",
            "published_at": None, "asset_name": "appX-v1.1.0.tar.gz",
            "asset_url": "https://x/appX.tar.gz", "available": 1, "compatible": 1,
            "reason": "", "last_checked": db.now()})
        self.conn.commit()

    def test_install_all_runs_without_crashing(self):
        import io
        from contextlib import redirect_stdout
        from codehound.cli import main
        calls = []

        def fake_install(cfg, conn, repo, progress=None, exe_name=None):
            calls.append((repo, exe_name))
            return f"{repo}: installed"

        for target, value in (
            ("codehound.cli.ensure_config", lambda: None),
            ("codehound.cli.load_config", lambda: _Cfg()),
            ("codehound.updates.self_repo", lambda cfg: ""),
            ("codehound.updates.install_update", fake_install),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["updates", "--install-all"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("zacpr/appX", None)])
        self.assertIn("installed 1 update(s)", buf.getvalue())

    def test_install_all_forwards_exe(self):
        import io
        from contextlib import redirect_stdout
        from codehound.cli import main
        calls = []

        def fake_install(cfg, conn, repo, progress=None, exe_name=None):
            calls.append((repo, exe_name))
            return f"{repo}: installed"

        for target, value in (
            ("codehound.cli.ensure_config", lambda: None),
            ("codehound.cli.load_config", lambda: _Cfg()),
            ("codehound.updates.self_repo", lambda cfg: ""),
            ("codehound.updates.install_update", fake_install),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = main(["updates", "--install-all", "--exe", "appX"])
        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("zacpr/appX", "appX")])


if __name__ == "__main__":
    unittest.main()
