"""Tests for the transparent scored keep-decision (process/decision.py)."""

from process.decision import DecisionRule, clamp_axes, decide, rule_from_cfg

DEFAULT = DecisionRule()


def _axes(magnitude=0, dissonance=0, credibility=3, redundancy=0, relevance=0):
    return {
        "magnitude": magnitude,
        "dissonance": dissonance,
        "credibility": credibility,
        "redundancy": redundancy,
        "relevance": relevance,
    }


# --- clamp_axes ---


def test_clamp_fills_missing_with_zero():
    assert clamp_axes({}) == dict.fromkeys(
        ("magnitude", "dissonance", "credibility", "redundancy", "relevance"), 0
    )


def test_clamp_bounds_and_coerces():
    got = clamp_axes({"magnitude": 9, "dissonance": -2, "credibility": "2", "redundancy": None})
    assert got["magnitude"] == 3
    assert got["dissonance"] == 0
    assert got["credibility"] == 2
    assert got["redundancy"] == 0


# --- decide ---


def test_magnitude_carries():
    assert decide(_axes(magnitude=2), DEFAULT) is True


def test_dissonance_carries_when_credible():
    assert decide(_axes(dissonance=3, magnitude=0), DEFAULT) is True


def test_nothing_clears_floor_fails():
    assert decide(_axes(magnitude=1, dissonance=1, relevance=1), DEFAULT) is False


def test_redundancy_veto_overrides_strong_signal():
    # A high-magnitude story that is "more of the same" is dropped.
    assert decide(_axes(magnitude=3, redundancy=2), DEFAULT) is False


def test_credibility_gate_blocks_dissonant_rumor():
    # Strong structural surprise but only a rumor -> fail.
    assert decide(_axes(dissonance=3, credibility=0), DEFAULT) is False


def test_veto_disabled_lets_redundant_through():
    rule = DecisionRule(redundancy_veto=4)  # axes max at 3, so never vetoes
    assert decide(_axes(magnitude=3, redundancy=3), rule) is True


# --- rule_from_cfg ---


def test_rule_off_by_default():
    assert rule_from_cfg({}) is None
    assert rule_from_cfg({"decision": {"mode": "llm"}}) is None


def test_rule_built_from_scored_cfg():
    rule = rule_from_cfg({"decision": {"mode": "scored", "keep_floor": 3, "redundancy_veto": 1}})
    assert rule == DecisionRule(keep_floor=3, redundancy_veto=1, min_credibility=1)
