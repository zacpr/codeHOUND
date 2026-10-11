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


if __name__ == "__main__":
    unittest.main()
