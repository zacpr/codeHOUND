"""Checks for the systemd unit self-heal: an upgraded codeHOUND must refresh
per-user units left behind by the old package.
Run with QT_QPA_PLATFORM=offscreen python -m unittest discover -s tests."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codehound import install


class _Base(unittest.TestCase):
    """Scratch systemd dir; systemctl calls stubbed out."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.units = Path(self.temp.name) / "user"
        patcher = patch.object(install, "SYSTEMD_DIR", self.units)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        run_patcher = patch.object(
            install.subprocess, "run",
            lambda cmd, **kw: self.calls.append(cmd) or _Ok())
        run_patcher.start()
        self.addCleanup(run_patcher.stop)
        self.noop = lambda m: None

    def write_unit(self, name, body):
        self.units.mkdir(parents=True, exist_ok=True)
        (self.units / name).write_text(body, encoding="utf-8")


class _Ok:
    returncode = 0
    stderr = ""


class MarkerTests(_Base):
    def test_fresh_install_writes_marker(self):
        self.assertEqual(install.install_timer(progress=self.noop), 0)
        self.assertTrue(install.units_current())
        body = (self.units / "codehound-scan.service").read_text()
        self.assertIn(f"{install._UNIT_MARK} {install.UNIT_VERSION}", body)

    def test_new_unit_still_checks_for_updates(self):
        install.install_timer(progress=self.noop)
        body = (self.units / "codehound-scan.service").read_text()
        self.assertIn("updates --check", body)

    def test_unit_written_before_markers_existed_is_stale(self):
        # the real 0.2.0/0.4.0 unit: no marker, no updates line
        self.write_unit("codehound-scan.service",
                        "[Unit]\nDescription=codeHOUND project scan\n"
                        "[Service]\nType=oneshot\nExecStart=/usr/bin/codehound scan\n")
        self.write_unit("codehound-scan.timer",
                        "[Timer]\nOnBootSec=5min\nOnUnitActiveSec=6h\n")
        self.assertFalse(install.units_current())

    def test_missing_units_are_stale(self):
        self.assertFalse(install.units_current())

    def test_garbage_marker_is_stale(self):
        self.write_unit("codehound-scan.service",
                        f"{install._UNIT_MARK} banana\n")
        self.write_unit("codehound-scan.timer",
                        f"{install._UNIT_MARK} {install.UNIT_VERSION}\n")
        self.assertFalse(install.units_current())


class SelfHealTests(_Base):
    def test_ensure_units_rewrites_stale_unit(self):
        self.write_unit("codehound-scan.service",
                        "[Service]\nExecStart=/usr/bin/codehound scan\n")
        self.write_unit("codehound-scan.timer", "[Timer]\nOnUnitActiveSec=6h\n")
        msgs = []
        self.assertTrue(install.ensure_units(progress=msgs.append))
        self.assertTrue(install.units_current())
        self.assertIn("updates --check",
                      (self.units / "codehound-scan.service").read_text())
        self.assertTrue(any("refreshing" in m for m in msgs))
        # reloaded so systemd picks up the new ExecStart line
        self.assertIn(["systemctl", "--user", "daemon-reload"], self.calls)

    def test_ensure_units_is_quiet_when_current(self):
        install.install_timer(progress=self.noop)
        msgs = []
        self.calls.clear()
        self.assertFalse(install.ensure_units(progress=msgs.append))
        self.assertEqual(self.calls, [])
        self.assertEqual(msgs, [])

    def test_second_call_is_idempotent(self):
        self.write_unit("codehound-scan.service", "[Service]\nExecStart=x\n")
        self.write_unit("codehound-scan.timer", "[Timer]\n")
        self.assertTrue(install.ensure_units(progress=self.noop))
        self.assertFalse(install.ensure_units(progress=self.noop))

    def test_failure_to_reload_never_raises(self):
        self.write_unit("codehound-scan.service", "[Service]\nExecStart=x\n")
        self.write_unit("codehound-scan.timer", "[Timer]\n")

        class _Bad:
            returncode = 1
            stderr = "Failed to connect to bus"

        with patch.object(install.subprocess, "run",
                          lambda cmd, **kw: _Bad()):
            msgs = []
            self.assertFalse(install.ensure_units(progress=msgs.append))
        self.assertTrue(any("by hand" in m for m in msgs))

    def test_unexpected_error_is_swallowed(self):
        with patch.object(install, "units_current",
                          side_effect=RuntimeError("boom")):
            self.assertFalse(install.ensure_units(progress=self.noop))


if __name__ == "__main__":
    unittest.main()
