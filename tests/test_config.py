"""Config round-trip checks. Run with QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests."""
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from codehound import config


class SaveConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for target, value in (
            ("codehound.config.CONFIG_DIR", root),
            ("codehound.config.CONFIG_PATH", root / "config.toml"),
        ):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_launcher_name_with_spaces_survives_reload(self):
        cfg = config.load_config()
        cfg.launchers = {
            "editor": "editor {path}",
            "Claude Code": "konsole --workdir {path} -e claude",
        }
        config.save_config(cfg)

        raw = tomllib.loads(config.CONFIG_PATH.read_text())
        self.assertEqual(
            raw["launchers"]["Claude Code"],
            "konsole --workdir {path} -e claude",
        )
        self.assertEqual(
            config.load_config().launchers["Claude Code"],
            "konsole --workdir {path} -e claude",
        )


if __name__ == "__main__":
    unittest.main()
