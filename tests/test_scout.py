"""Tests for scout pipeline: parse_scout_result in AgentLauncher."""

import pytest

from agent_grid.coordinator.agent_launcher import AgentLauncher


class TestParseScoutResult:
    def _make_launcher(self):
        launcher = AgentLauncher.__new__(AgentLauncher)
        return launcher

    def test_valid_result(self):
        launcher = self._make_launcher()
        text = '''Some exploration output...
<!-- SCOUT_RESULT -->
```json
{"verdict": "implement", "plan": "Do X then Y", "reason": "Simple change"}
```
<!-- /SCOUT_RESULT -->'''
        result = launcher.parse_scout_result(text)
        assert result is not None
        assert result["verdict"] == "implement"
        assert result["plan"] == "Do X then Y"

    def test_no_marker(self):
        launcher = self._make_launcher()
        assert launcher.parse_scout_result("just some text") is None

    def test_empty_string(self):
        launcher = self._make_launcher()
        assert launcher.parse_scout_result("") is None

    def test_none_input(self):
        launcher = self._make_launcher()
        assert launcher.parse_scout_result(None) is None

    def test_no_end_marker(self):
        launcher = self._make_launcher()
        text = '''<!-- SCOUT_RESULT -->
```json
{"verdict": "decompose", "steps": [{"title": "Step 1"}]}
```'''
        result = launcher.parse_scout_result(text)
        assert result is not None
        assert result["verdict"] == "decompose"

    def test_without_code_fences(self):
        launcher = self._make_launcher()
        text = '''<!-- SCOUT_RESULT -->
{"verdict": "needs_human", "question": "What API key?"}
<!-- /SCOUT_RESULT -->'''
        result = launcher.parse_scout_result(text)
        assert result is not None
        assert result["verdict"] == "needs_human"
