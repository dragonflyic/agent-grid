"""Tests for the quality gate module."""

from agent_grid.config import settings
from agent_grid.coordinator.quality_gate import ConfidenceAssessment, QualityGate


class TestConfidenceAssessment:
    """Tests for the ConfidenceAssessment data class."""

    def test_create_assessment(self):
        assessment = ConfidenceAssessment(
            score=9,
            verdict="proceed",
            risk_flags=[],
            green_flags=["exact_files_specified"],
            explanation="Clear issue with specific files",
        )
        assert assessment.score == 9
        assert assessment.verdict == "proceed"
        assert assessment.clarification_question is None

    def test_assessment_with_clarification(self):
        assessment = ConfidenceAssessment(
            score=4,
            verdict="clarify",
            risk_flags=["vague_description"],
            green_flags=[],
            explanation="Issue is too vague",
            clarification_question="What files should be changed?",
        )
        assert assessment.verdict == "clarify"
        assert assessment.clarification_question is not None


class TestQualityGateDecisions:
    """Tests for should_proceed and should_clarify logic."""

    def _make_gate(self):
        """Create a QualityGate without triggering singleton initialization."""
        gate = QualityGate.__new__(QualityGate)
        return gate

    def test_high_confidence_proceeds_for_opted_in(self):
        gate = self._make_gate()
        assessment = ConfidenceAssessment(
            score=9, verdict="proceed", risk_flags=[], green_flags=["clear"],
            explanation="Clear",
        )
        assert gate.should_proceed(assessment, is_proactive=False) is True

    def test_moderate_confidence_proceeds_for_opted_in(self):
        gate = self._make_gate()
        assessment = ConfidenceAssessment(
            score=7, verdict="proceed", risk_flags=[], green_flags=[],
            explanation="Mostly clear",
        )
        assert gate.should_proceed(assessment, is_proactive=False) is True

    def test_proactive_requires_high_score(self):
        """Proactive pickup needs score >= proactive_min_score (default 9)."""
        gate = self._make_gate()
        assessment = ConfidenceAssessment(
            score=8, verdict="proceed", risk_flags=[], green_flags=[],
            explanation="Good but not great",
        )
        assert gate.should_proceed(assessment, is_proactive=True) is False

    def test_proactive_proceeds_at_min_score(self):
        gate = self._make_gate()
        assessment = ConfidenceAssessment(
            score=settings.proactive_min_score, verdict="proceed",
            risk_flags=[], green_flags=[], explanation="Confident",
        )
        assert gate.should_proceed(assessment, is_proactive=True) is True

    def test_proactive_requires_proceed_verdict(self):
        """Even with score 10, verdict must be 'proceed' for proactive."""
        gate = self._make_gate()
        assessment = ConfidenceAssessment(
            score=10, verdict="clarify", risk_flags=[], green_flags=[],
            explanation="Needs clarification despite high score",
        )
        assert gate.should_proceed(assessment, is_proactive=True) is False

    def test_clarify_verdict_blocks_opted_in(self):
        gate = self._make_gate()
        assessment = ConfidenceAssessment(
            score=5, verdict="clarify", risk_flags=["vague"],
            green_flags=[], explanation="Vague",
            clarification_question="What exactly?",
        )
        assert gate.should_clarify(assessment, is_proactive=False) is True

    def test_proactive_never_clarifies(self):
        """Proactive scan should never spam issues with clarification questions."""
        gate = self._make_gate()
        assessment = ConfidenceAssessment(
            score=5, verdict="clarify", risk_flags=["vague"],
            green_flags=[], explanation="Vague",
            clarification_question="What exactly?",
        )
        assert gate.should_clarify(assessment, is_proactive=True) is False

    def test_skip_verdict_does_not_proceed(self):
        gate = self._make_gate()
        assessment = ConfidenceAssessment(
            score=2, verdict="skip", risk_flags=["too_risky"],
            green_flags=[], explanation="Too risky",
        )
        assert gate.should_proceed(assessment, is_proactive=False) is False

    def test_skip_verdict_does_not_clarify(self):
        gate = self._make_gate()
        assessment = ConfidenceAssessment(
            score=2, verdict="skip", risk_flags=["too_risky"],
            green_flags=[], explanation="Too risky",
        )
        assert gate.should_clarify(assessment, is_proactive=False) is False


class TestParseFailureSafety:
    """Tests for safe defaults on parse failures."""

    def test_default_assessment_is_safe(self):
        """When Claude returns garbage, default should be clarify, not proceed."""
        # Simulated parse failure default
        assessment = ConfidenceAssessment(
            score=5,
            verdict="clarify",
            risk_flags=["parse_error"],
            green_flags=[],
            explanation="Quality gate parse error",
            clarification_question="Could you clarify the scope?",
        )
        gate = QualityGate.__new__(QualityGate)
        assert gate.should_proceed(assessment, is_proactive=False) is False
        assert gate.should_proceed(assessment, is_proactive=True) is False
        assert gate.should_clarify(assessment, is_proactive=False) is True
