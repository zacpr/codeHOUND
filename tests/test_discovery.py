"""Unit tests for codehound.discovery (stdlib-only, no Qt needed)."""
import json
import unittest
from unittest.mock import patch

from codehound import discovery


class DetectEditorsTests(unittest.TestCase):
    def test_finds_installed_binaries(self):
        with patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}" if b == "code" else None):
            found = discovery.detect_editors({})
        self.assertEqual([d.name for d in found], ["VS Code"])
        self.assertEqual(found[0].command, "code {path}")

    def test_excludes_already_configured_binary(self):
        with patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}" if b == "code" else None):
            found = discovery.detect_editors({"editor": "code {path}"})
        self.assertEqual(found, [])

    def test_none_installed(self):
        with patch("shutil.which", return_value=None):
            self.assertEqual(discovery.detect_editors({}), [])


class DetectAgentClisTests(unittest.TestCase):
    def test_no_terminal_available_yields_nothing(self):
        with patch("shutil.which", return_value=None):
            self.assertEqual(discovery.detect_agent_clis({}), [])

    def test_wraps_agent_in_first_available_terminal(self):
        def which(binary):
            if binary in ("konsole", "claude"):
                return f"/usr/bin/{binary}"
            return None

        with patch("shutil.which", side_effect=which):
            found = discovery.detect_agent_clis({})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].name, "Claude Code")
        self.assertEqual(found[0].command, "konsole --workdir {path} -e claude")

    def test_excludes_already_configured_binary(self):
        def which(binary):
            if binary in ("konsole", "claude"):
                return f"/usr/bin/{binary}"
            return None

        with patch("shutil.which", side_effect=which):
            found = discovery.detect_agent_clis({"agent": "konsole --workdir {path} -e claude"})
        self.assertEqual(found, [])


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._body = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class DetectModelRunnersTests(unittest.TestCase):
    def test_reachable_service_with_model(self):
        def urlopen(req, timeout):
            if "1234" in req.full_url:
                return _FakeResponse({"data": [{"id": "some-model"}]})
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", side_effect=urlopen):
            found = discovery.detect_model_runners(timeout=0.1)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].key, "lmstudio")
        self.assertEqual(found[0].model, "some-model")

    def test_nothing_reachable(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            found = discovery.detect_model_runners(timeout=0.1)
        self.assertEqual(found, [])

    def test_timeout_is_treated_as_unreachable(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError):
            found = discovery.detect_model_runners(timeout=0.1)
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
