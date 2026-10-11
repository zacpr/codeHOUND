"""Phase 4 checks: system package installs (pkexec) and codeHOUND self-update.
Run with QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codehound import db
from codehound.updates import (
    check_self_update,
    schedule_relaunch,
    self_repo,
    self_update,
    system_package_install,
)
from codehound.providers.base import SyncError


class _Cfg:
    accounts: dict = {}
    updates_enabled = True
    updates_mode = "stage"
    self_repo = "zacpr/codeHOUND"


class _Base(unittest.TestCase):
    """Scratch data dir + catalogue DB, download patched out — like the
    phase-2 install tests, from a throwaway cwd so nothing can wipe anything."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target, value in (
            ("codehound.db.DB_PATH", self.root / "cat.db"),
            ("codehound.updates.DATA_DIR", self.root / "data"),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.conn = db.connect()
        self.addCleanup(self.conn.close)
        self.noop = lambda m: None

    def _seed(self, repo, asset, url=None, available=1, compatible=1):
        db.upsert_update(self.conn, {
            "repo": repo, "path": "/r/x", "name": repo.split("/")[1],
            "current_tag": "v0.1.0", "latest_tag": "v0.2.0",
            "published_at": None, "asset_name": asset,
            "asset_url": url or f"https://x/{asset}",
            "available": available, "compatible": compatible,
            "reason": "", "last_checked": db.now()})
        self.conn.commit()


class SystemInstallTests(_Base):
    def setUp(self):
        super().setUp()
        self._seed("zacpr/appX", "appX-1.1.0.fc44.x86_64.rpm")
        # the fake download drops a package file on disk
        self.pkg = self.root / "appX-1.1.0.fc44.x86_64.rpm"

        def fake_download(url, dest, headers=None):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"fake rpm payload")
            return dest
        patcher = patch("codehound.updates.download_asset", side_effect=fake_download)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("codehound.updates._release_by_tag",
                        return_value={"assets": [], "body": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cmds = []

        def fake_run(cmd, timeout=600):
            self.cmds.append(list(cmd))
            return ""
        patcher = patch("codehound.updates.run", side_effect=fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("codehound.updates.shutil.which",
                        side_effect=lambda c: f"/usr/bin/{c}" if c in ("pkexec", "dnf") else None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_pkexec_dnf_install_and_db_row(self):
        summary = system_package_install(_Cfg(), self.conn, "zacpr/appX",
                                         progress=self.noop)
        self.assertEqual(len(self.cmds), 1)
        cmd = self.cmds[0]
        self.assertEqual(cmd[:3], ["pkexec", "dnf", "install"])
        self.assertTrue(str(cmd[-1]).endswith("appX-1.1.0.fc44.x86_64.rpm"))
        row = db.get_update(self.conn, "zacpr/appX")
        self.assertEqual(row["install_mode"], "system")
        self.assertEqual(row["installed_tag"], "v0.2.0")
        self.assertEqual(row["available"], 0)
        self.assertIn("installed", summary)

    def test_archive_deleted_after_success(self):
        system_package_install(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        left = [p for p in (self.root / "data" / "downloads").iterdir()]
        self.assertEqual(left, [])

    def test_non_package_asset_refused(self):
        self._seed("zacpr/appY", "appY-x86_64.AppImage")
        with self.assertRaises(SyncError) as ctx:
            system_package_install(_Cfg(), self.conn, "zacpr/appY", progress=self.noop)
        self.assertIn("not an RPM or DEB", str(ctx.exception))
        self.assertEqual(self.cmds, [])

    def test_no_pkexec_refused(self):
        patcher = patch("codehound.updates.shutil.which", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(SyncError) as ctx:
            system_package_install(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        self.assertIn("pkexec", str(ctx.exception))
        self.assertEqual(self.cmds, [])

    def test_installer_failure_keeps_row_uninstalled(self):
        def fail_run(cmd, timeout=600):
            raise SyncError("pkexec: authentication failure")
        patcher = patch("codehound.updates.run", side_effect=fail_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.assertRaises(SyncError) as ctx:
            system_package_install(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        self.assertIn("system install failed", str(ctx.exception))
        row = db.get_update(self.conn, "zacpr/appX")
        self.assertEqual(row["install_mode"] or "", "")
        self.assertEqual(row["available"], 1)

    def test_system_mode_defers_rollback_to_pkg_manager(self):
        system_package_install(_Cfg(), self.conn, "zacpr/appX", progress=self.noop)
        from codehound.updates import rollback_update
        with self.assertRaises(SyncError) as ctx:
            rollback_update(self.conn, "zacpr/appX", progress=self.noop)
        self.assertIn("package manager", str(ctx.exception))

    def test_installer_choice_per_format(self):
        from codehound.updates import _package_installer
        which = {c: f"/usr/bin/{c}" for c in ("dnf", "apt-get", "dpkg", "rpm")}
        patcher = patch("codehound.updates.shutil.which",
                        side_effect=lambda c: which.get(c))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertEqual(_package_installer(Path("a.rpm")), ["dnf", "install", "-y"])
        self.assertEqual(_package_installer(Path("a.deb")), ["apt-get", "install", "-y"])
        with self.assertRaises(SyncError):
            _package_installer(Path("a.tar.gz"))


class SelfUpdateTests(_Base):
    def setUp(self):
        super().setUp()
        patcher = patch("codehound.updates.__version__", "0.4.0")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _release(self, tag, asset):
        return {"tag_name": tag, "published_at": None,
                "assets": [{"name": asset,
                            "browser_download_url": f"https://x/{asset}"}]}

    def test_check_self_update_compares_installed_version(self):
        patcher = patch("codehound.updates._latest_release",
                        return_value=self._release("v0.5.0", "codehound-0.5.0-x86_64.rpm"))
        patcher.start()
        self.addCleanup(patcher.stop)
        row = check_self_update(_Cfg(), self.conn, progress=self.noop)
        self.assertEqual(row["repo"], "zacpr/codeHOUND")
        self.assertEqual(row["current_tag"], "0.4.0")
        self.assertEqual(row["latest_tag"], "v0.5.0")
        self.assertEqual(row["available"], 1)
        self.assertEqual(row["compatible"], 1)

    def test_up_to_date_not_flagged(self):
        patcher = patch("codehound.updates._latest_release",
                        return_value=self._release("v0.4.0", "codehound-0.4.0-x86_64.rpm"))
        patcher.start()
        self.addCleanup(patcher.stop)
        row = check_self_update(_Cfg(), self.conn, progress=self.noop)
        self.assertEqual(row["available"], 0)

    def test_check_self_update_stamps_clone_path(self):
        """The row's path must be the catalogued clone so the dashboard
        (which keys updates by path) shows the ⬆ badge on our own row."""
        db.upsert_project(self.conn, {
            "path": "/r/codeHOUND", "kind": "git", "name": "codeHOUND",
            "root": "/r", "language": "Python", "branch": "main",
            "remote_url": "https://github.com/zacpr/codeHOUND.git",
            "last_commit_date": None, "last_commit_msg": None, "dirty": 0,
            "created": None, "modified": None, "size_approx": None})
        db.upsert_project(self.conn, {
            "path": "/r/other", "kind": "git", "name": "other",
            "root": "/r", "language": "Python", "branch": "main",
            "remote_url": "https://github.com/zacpr/other.git",
            "last_commit_date": None, "last_commit_msg": None, "dirty": 0,
            "created": None, "modified": None, "size_approx": None})
        self.conn.commit()
        patcher = patch("codehound.updates._latest_release",
                        return_value=self._release("v0.5.0", "codehound-0.5.0-x86_64.rpm"))
        patcher.start()
        self.addCleanup(patcher.stop)
        row = check_self_update(_Cfg(), self.conn, progress=self.noop)
        self.assertEqual(row["path"], "/r/codeHOUND")

    def test_check_self_update_without_clone_keeps_path_blank(self):
        patcher = patch("codehound.updates._latest_release",
                        return_value=self._release("v0.5.0", "codehound-0.5.0-x86_64.rpm"))
        patcher.start()
        self.addCleanup(patcher.stop)
        row = check_self_update(_Cfg(), self.conn, progress=self.noop)
        self.assertEqual(row["path"], "")

    def test_repo_without_releases_returns_none(self):
        patcher = patch("codehound.updates._latest_release", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertIsNone(check_self_update(_Cfg(), self.conn, progress=self.noop))

    def test_self_repo_blank_disables(self):
        cfg = _Cfg()
        cfg.self_repo = ""
        self.assertEqual(self_repo(cfg), "")
        with self.assertRaises(SyncError) as ctx:
            self_update(cfg, self.conn, progress=self.noop)
        self.assertIn("self_repo", str(ctx.exception))

    def test_self_update_installs_package_via_pkexec(self):
        self._seed("zacpr/codeHOUND", "codehound-0.5.0-1.fc44.x86_64.rpm")
        patcher = patch("codehound.updates.download_asset",
                        side_effect=lambda url, dest, headers=None:
                        (dest.parent.mkdir(parents=True, exist_ok=True),
                         dest.write_bytes(b"pkg"), dest)[-1])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("codehound.updates._release_by_tag",
                        return_value={"assets": [], "body": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        cmds = []
        patcher = patch("codehound.updates.run",
                        side_effect=lambda cmd, timeout=600: cmds.append(list(cmd)) or "")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("codehound.updates.shutil.which",
                        side_effect=lambda c: f"/usr/bin/{c}" if c in ("pkexec", "dnf") else None)
        patcher.start()
        self.addCleanup(patcher.stop)
        summary = self_update(_Cfg(), self.conn, progress=self.noop)
        self.assertEqual(cmds[0][:3], ["pkexec", "dnf", "install"])
        self.assertIn("restart codeHOUND", summary)
        row = db.get_update(self.conn, "zacpr/codeHOUND")
        self.assertEqual(row["install_mode"], "system")
        self.assertEqual(row["installed_tag"], "v0.2.0")

    def test_self_update_stages_non_package_asset(self):
        self._seed("zacpr/codeHOUND", "codehound-0.5.0-x86_64.appimage")
        patcher = patch("codehound.updates.download_asset",
                        side_effect=lambda url, dest, headers=None:
                        (dest.parent.mkdir(parents=True, exist_ok=True),
                         dest.write_bytes(b"#!/bin/sh\n"), dest)[-1])
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("codehound.updates._release_by_tag",
                        return_value={"assets": [], "body": ""})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("codehound.updates._gh_token", return_value="")
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("codehound.updates.run",
                        side_effect=AssertionError("must not run pkexec"))
        patcher.start()
        self.addCleanup(patcher.stop)
        summary = self_update(_Cfg(), self.conn, progress=self.noop)
        self.assertIn("staged", summary)
        row = db.get_update(self.conn, "zacpr/codeHOUND")
        self.assertTrue(row["staged_dir"])

    def test_self_update_already_current(self):
        self._seed("zacpr/codeHOUND", "codehound-0.4.0-x86_64.rpm",
                   available=0, compatible=0)
        patcher = patch("codehound.updates._latest_release",
                        return_value=self._release("v0.4.0", "codehound-0.4.0-x86_64.rpm"))
        patcher.start()
        self.addCleanup(patcher.stop)
        summary = self_update(_Cfg(), self.conn, progress=self.noop)
        self.assertIn("already the newest", summary)

    def test_policy_pass_never_auto_installs_self(self):
        """apply_updates must not swap a running app under itself."""
        from codehound.updates import apply_updates
        self._seed("zacpr/codeHOUND", "codehound-0.5.0-x86_64.appimage")
        cfg = _Cfg()
        cfg.updates_mode = "silent"
        installed, staged, errors = apply_updates(cfg, self.conn, progress=self.noop)
        self.assertEqual(installed, [])
        self.assertEqual(staged, [])
        self.assertEqual(errors, [])

    def test_check_updates_skips_own_row(self):
        """watch_targets may include our clone; check_self_update owns the row."""
        from codehound.updates import check_updates
        db.upsert_project(self.conn, {
            "path": "/r/codeHOUND", "kind": "git", "name": "codeHOUND",
            "root": "/r", "language": "Python", "branch": "main",
            "remote_url": "https://github.com/zacpr/codeHOUND.git",
            "last_commit_date": None, "last_commit_msg": None, "dirty": 0,
            "created": None, "modified": None, "size_approx": None})
        self.conn.commit()
        calls = []
        patcher = patch("codehound.updates._latest_release",
                        side_effect=lambda cfg, repo: calls.append(repo) or None)
        patcher.start()
        self.addCleanup(patcher.stop)
        check_updates(_Cfg(), self.conn, progress=self.noop)
        self.assertEqual(calls, [])   # the self row is not fetched by the generic check


class ManagedInstallHardeningTests(_Base):
    """A tampered catalogue row must not steer managed installs outside the
    installs tree (the wipe-bug class: DB strings used as path components)."""

    def test_absolute_tag_refused(self):
        from codehound.updates import _install_managed
        self._seed("zacpr/appX", "appX.tar.gz")
        row = db.get_update(self.conn, "zacpr/appX")
        row = dict(row) | {"latest_tag": "/etc"}
        stage = self.root / "stage"
        stage.mkdir()
        with self.assertRaises(SyncError) as ctx:
            _install_managed(self.conn, row, stage, stage / "appX", self.noop)
        self.assertIn("unsafe", str(ctx.exception))

    def test_traversal_tag_refused(self):
        from codehound.updates import _install_managed
        self._seed("zacpr/appX", "appX.tar.gz")
        row = dict(db.get_update(self.conn, "zacpr/appX")) | {"latest_tag": ".."}
        stage = self.root / "stage"
        stage.mkdir()
        with self.assertRaises(SyncError):
            _install_managed(self.conn, row, stage, stage / "appX", self.noop)

    def test_slug_never_escapes_user_bin(self):
        from codehound.updates import slug
        self.assertNotIn("/", slug("owner/a/../b"))
        self.assertNotIn("\\", slug("owner/a\\b"))


class RelaunchTests(_Base):
    def test_schedule_relaunch_detaches_helper(self):
        spawned = []
        patcher = patch("codehound.updates.subprocess.Popen",
                        side_effect=lambda cmd, **kw: spawned.append((cmd, kw)))
        patcher.start()
        self.addCleanup(patcher.stop)
        msg = schedule_relaunch(when_pid=4242, launch=["codehound", "app"],
                                progress=self.noop)
        self.assertEqual(len(spawned), 1)
        cmd, kw = spawned[0]
        self.assertEqual(cmd[0], "sh")
        script = cmd[2]
        self.assertIn("kill -0 4242", script)
        self.assertIn("exec codehound app", script)
        self.assertTrue(kw.get("start_new_session"))
        self.assertIn("relaunch scheduled", msg)

    def test_default_launch_is_the_package_module(self):
        spawned = []
        patcher = patch("codehound.updates.subprocess.Popen",
                        side_effect=lambda cmd, **kw: spawned.append(cmd))
        patcher.start()
        self.addCleanup(patcher.stop)
        schedule_relaunch(when_pid=1, progress=self.noop)
        self.assertIn("-m codehound.cli app", spawned[0][2])
