"""Tests for the safe_rmtree chokepoint — every delete goes through it."""
import os
import tempfile
import unittest
from pathlib import Path

from codehound.safepath import UnsafePathError, check_path, forbidden_dirs, safe_rmtree


class CheckPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.allowed = (self.root / "data",)
        self.allowed[0].mkdir()

    def _mk(self, rel="data/victim") -> Path:
        p = self.root / rel
        p.mkdir(parents=True)
        (p / "file").write_text("x")
        return p

    def test_empty_path_refused(self):
        with self.assertRaises(UnsafePathError):
            check_path("", self.allowed, "t")

    def test_whitespace_path_refused(self):
        with self.assertRaises(UnsafePathError):
            check_path("   ", self.allowed, "t")

    def test_relative_path_refused(self):
        with self.assertRaises(UnsafePathError):
            check_path("data/victim", self.allowed, "t")

    def test_dot_path_refused(self):
        # Path(".") is cwd — same failure mode as Path("")
        with self.assertRaises(UnsafePathError):
            check_path(".", self.allowed, "t")

    def test_root_itself_refused(self):
        victim = self._mk()
        with self.assertRaises(UnsafePathError):
            check_path(self.allowed[0], self.allowed, "t")

    def test_outside_roots_refused(self):
        outside = Path(self.temp.name).parent / "not-mine"
        with self.assertRaises(UnsafePathError):
            check_path(outside, self.allowed, "t")

    def test_blacklisted_dirs_refused(self):
        # even a caller that claims them as roots cannot delete them
        for bad in ["/", str(Path.home()), "/home", "/bin", "/etc", "/usr", "/var"]:
            with self.subTest(bad=bad), self.assertRaises(UnsafePathError):
                check_path(bad, (bad,), "t")

    def test_blacklist_covers_home_variants(self):
        home = Path.home().resolve()
        self.assertIn(home, forbidden_dirs())
        # the home dir itself is refused even when a caller claims it as a root
        with self.assertRaises(UnsafePathError):
            check_path(home, (home,), "t")
        # ...but its contents are deletable under containment (our data dirs
        # live under ~/.local/share), while siblings of the user are not
        with self.assertRaises(UnsafePathError):
            check_path(home.parent / "other-user", (home.parent,), "t")

    def test_symlink_escape_refused(self):
        victim = self._mk("data/linkme")
        link = self.root / "data" / "sneaky"
        outside = self.root / "outside"
        outside.mkdir()
        link.symlink_to(outside)
        with self.assertRaises(UnsafePathError):
            check_path(link, self.allowed, "t")
        self.assertTrue(outside.is_dir())

    def test_parent_traversal_refused(self):
        with self.assertRaises(UnsafePathError):
            check_path(self.allowed[0] / "..", self.allowed, "t")


class SafeRmtreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.allowed = (self.root / "data",)
        self.allowed[0].mkdir()

    def test_deletes_inside_root(self):
        victim = self.allowed[0] / "victim"
        victim.mkdir()
        (victim / "f").write_text("x")
        safe_rmtree(victim, self.allowed, "t")
        self.assertFalse(victim.exists())

    def test_missing_path_is_noop(self):
        safe_rmtree(self.allowed[0] / "nope", self.allowed, "t")

    def test_file_target_refused(self):
        f = self.allowed[0] / "file"
        f.write_text("x")
        with self.assertRaises(UnsafePathError):
            safe_rmtree(f, self.allowed, "t")
        self.assertTrue(f.is_file())

    def test_cwd_sentinel_survives_empty_path(self):
        # the 2026-10-11 failure mode, straight at the chokepoint
        cwd = Path(tempfile.mkdtemp())
        sentinel = cwd / "SENTINEL"
        sentinel.write_text("keep me")
        old = Path.cwd()
        os.chdir(cwd)
        self.addCleanup(os.chdir, old)
        with self.assertRaises(UnsafePathError):
            safe_rmtree("", self.allowed, "t")
        self.assertTrue(sentinel.is_file())


class CallSitesUseChokepointTests(unittest.TestCase):
    """No module may call shutil.rmtree directly any more."""

    def test_no_direct_rmtree_calls(self):
        pkg = Path(__file__).resolve().parent.parent / "codehound"
        offenders = []
        for py in pkg.rglob("*.py"):
            if py.name == "safepath.py":
                continue
            text = py.read_text()
            if "shutil.rmtree" in text:
                offenders.append(py.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
