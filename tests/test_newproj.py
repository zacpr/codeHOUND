"""Tests for newproj (create / clone / worktree). Run with:

    python -m unittest discover -s tests

Needs git on PATH; skipped entirely if git is missing.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codehound import config, db, newproj


def _have_git() -> bool:
    return shutil.which("git") is not None


def run_git(cwd, *args):
    # core.hooksPath= keeps a developer's global commit hooks (e.g. a
    # "no direct commits to main" guard) from breaking test fixtures.
    subprocess.run(["git", "-C", str(cwd), "-c", "core.hooksPath=", *args],
                   check=True, capture_output=True, text=True)


class NewProjBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _have_git():
            raise unittest.SkipTest("git not on PATH")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for target, value in (
            ("codehound.config.CONFIG_DIR", root / "config"),
            ("codehound.config.CONFIG_PATH", root / "config" / "config.toml"),
            ("codehound.db.DATA_DIR", root / "data"),
            ("codehound.db.DB_PATH", root / "data" / "catalogue.db"),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.root = root
        self.cfg = config.load_config()
        self.cfg.adopt_dest = str(root / "repos")
        self.cfg.clone_dest = str(root / "repos")
        self.cfg.roots = [str(root)]
        # Keep a stable git identity inside the sandbox so commits work
        # regardless of the developer's global config.
        env_patcher = patch.dict(
            "os.environ",
            {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    def catalogue_paths(self):
        conn = db.connect()
        try:
            return {r["path"] for r in db.all_projects(conn)}
        finally:
            conn.close()


@unittest.skipUnless(_have_git(), "git not on PATH")
class CreateTests(NewProjBase):
    def test_create_makes_git_project_and_registers(self):
        dest = newproj.create_project(self.cfg, "widget", description="A test widget.")
        target = Path(dest)
        self.assertEqual(target.name, "widget")
        self.assertTrue((target / "README.md").exists())
        self.assertIn("A test widget.", (target / "README.md").read_text())
        self.assertTrue((target / ".git").exists())
        # exactly one commit, on main
        log = subprocess.run(["git", "-C", str(target), "log", "--oneline"],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(len(log.strip().splitlines()), 1)
        branch = subprocess.run(["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        self.assertIn(branch, ("main", "master"))
        # registered in the catalogue
        self.assertIn(str(target), self.catalogue_paths())

    def test_second_create_gets_unique_name(self):
        first = newproj.create_project(self.cfg, "widget")
        second = newproj.create_project(self.cfg, "widget")
        self.assertNotEqual(first, second)
        self.assertEqual(Path(second).name, "widget-2")

    def test_scaffold_falls_back_when_ai_unconfigured(self):
        # no provider -> llm_scaffold raises AIError inside create_project,
        # which must fall back to a plain README, not fail.
        self.cfg.ai_provider = "none"
        dest = newproj.create_project(self.cfg, "scaffolded", scaffold=True)
        self.assertTrue((Path(dest) / "README.md").exists())
        self.assertEqual(
            sorted(p.name for p in Path(dest).iterdir()),
            [".git", "README.md"],
        )

    def test_scaffold_writes_llm_files(self):
        self.cfg.ai_provider = "custom"
        self.cfg.ai_base_url = "http://localhost:1"
        self.cfg.ai_model = "m"
        reply = '```json\n{"main.py": "print(1)\\n", "docs/notes.md": "hi", "README.md": "# custom readme"}\n```'
        with patch.object(newproj, "get_backend") as get_be:
            get_be.return_value.ask.return_value = reply
            dest = newproj.create_project(self.cfg, "aifile", scaffold=True)
        self.assertTrue((Path(dest) / "main.py").exists())
        self.assertTrue((Path(dest) / "docs" / "notes.md").exists())
        # the model's README wins over the generated one
        self.assertEqual((Path(dest) / "README.md").read_text(), "# custom readme")
        # scaffold files made it into the initial commit
        files = subprocess.run(["git", "-C", dest, "ls-files"],
                               capture_output=True, text=True, check=True).stdout.split()
        self.assertIn("main.py", files)


@unittest.skipUnless(_have_git(), "git not on PATH")
class CloneTests(NewProjBase):
    def _make_origin(self) -> Path:
        origin = self.root / "origin-src"
        origin.mkdir()
        run_git(origin, "init", "-b", "main")
        (origin / "hello.txt").write_text("hi")
        run_git(origin, "add", "-A")
        run_git(origin, "commit", "-m", "first")
        return origin

    def test_clone_local_repo(self):
        origin = self._make_origin()
        dest = newproj.clone_project(self.cfg, str(origin))
        target = Path(dest)
        self.assertEqual(target.name, "origin-src")
        self.assertTrue((target / "hello.txt").exists())
        self.assertIn(str(target), self.catalogue_paths())
        remote = subprocess.run(["git", "-C", str(target), "remote", "get-url", "origin"],
                                capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(remote, str(origin))

    def test_clone_with_rename(self):
        origin = self._make_origin()
        dest = newproj.clone_project(self.cfg, str(origin), name="renamed")
        self.assertEqual(Path(dest).name, "renamed")

    def test_clone_bad_source_raises(self):
        with self.assertRaises(newproj.NewProjectError):
            newproj.clone_project(self.cfg, str(self.root / "not-a-repo"))


@unittest.skipUnless(_have_git(), "git not on PATH")
class WorktreeTests(NewProjBase):
    def setUp(self):
        super().setUp()
        self.src = self.root / "repos" / "srcproj"
        self.src.mkdir(parents=True)
        run_git(self.src, "init", "-b", "main")
        (self.src / "a.txt").write_text("a")
        run_git(self.src, "add", "-A")
        run_git(self.src, "commit", "-m", "base")

    def test_new_branch_worktree(self):
        dest = newproj.add_worktree(self.cfg, str(self.src), "feature-x")
        target = Path(dest)
        self.assertEqual(target.name, "srcproj-feature-x")
        self.assertEqual(target.parent, self.src.parent)
        branch = subprocess.run(["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(branch, "feature-x")
        self.assertIn(str(target), self.catalogue_paths())

    def test_existing_branch_worktree(self):
        run_git(self.src, "branch", "side")
        dest = newproj.add_worktree(self.cfg, str(self.src), "side")
        branch = subprocess.run(["git", "-C", dest, "rev-parse", "--abbrev-ref", "HEAD"],
                                capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(branch, "side")

    def test_bad_branch_name_raises(self):
        with self.assertRaises(newproj.NewProjectError):
            newproj.add_worktree(self.cfg, str(self.src), "bad name/..")

    def test_non_repo_raises(self):
        with self.assertRaises(newproj.NewProjectError):
            newproj.add_worktree(self.cfg, str(self.root), "whatever")


class HelperTests(unittest.TestCase):
    def test_unique_name_sanitises_and_suffixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "my-proj").mkdir()
            self.assertEqual(newproj.unique_name(base, "my proj"), "my-proj-2")
            self.assertEqual(newproj.unique_name(base, "weird!!name"), "weird-name")

    def test_default_branch_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            # not a repo at all -> "main"
            self.assertEqual(newproj.default_branch(tmp), "main")

    def test_parse_file_map_edge_cases(self):
        good = '{"a.txt": "x"}'
        self.assertEqual(newproj._parse_file_map(good), {"a.txt": "x"})
        fenced = "here you go:\n```json\n" + good + "\n```\nthanks!"
        self.assertEqual(newproj._parse_file_map(fenced), {"a.txt": "x"})
        # unparseable or unsafe input degrades to empty/dropped, never raises
        for bad in ("no json here", '"not even an object"', ""):
            self.assertEqual(newproj._parse_file_map(bad), {})
        unsafe = '{"../evil": "x", "/abs": "y", "a/../../b": "z", ".git/config": "w"}'
        self.assertEqual(newproj._parse_file_map(unsafe), {})

    def test_parse_file_map_size_guardrails(self):
        big = json.dumps({"a.txt": "x" * (newproj.MAX_FILE_CHARS + 100)})
        out = newproj._parse_file_map(big)
        self.assertEqual(len(out["a.txt"]), newproj.MAX_FILE_CHARS)
        many = json.dumps({f"f{i}.txt": "x" for i in range(newproj.MAX_FILES + 5)})
        self.assertLessEqual(len(newproj._parse_file_map(many)), newproj.MAX_FILES)
