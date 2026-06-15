"""Transparent keep-decision over the LLM's per-item newsworthiness axes.

Off by default: when a task's curate config has no `decision` block (or
`mode: llm`), the LLM's own holistic `passes` verdict is used unchanged. With
`mode: scored`, `decide()` recomputes the verdict from the ordinal axis scores
so the keep rule is legible and tunable without re-prompting the model.

The axes are scored 0-3 by the LLM (see `process.curate`); this module only
combines them. Keeping the rule here (not in the prompt) means the same axis
vector can later feed a learned model without touching the scoring step.
"""

from dataclasses import dataclass

# Every axis the LLM scores. `redundancy` is inverted: high = "more of the same".
AXES = ("magnitude", "dissonance", "credibility", "redundancy", "relevance")
# Axes that can independently carry an item over the keep bar.
CARRIERS = ("magnitude", "dissonance", "relevance")


@dataclass(frozen=True)
class DecisionRule:
    keep_floor: int = 2  # a carrier axis at/above this can keep the item
    redundancy_veto: int = 2  # redundancy at/above this fails regardless
    min_credibility: int = 1  # below this, fail regardless (claims, rumors)


def clamp_axes(raw: dict) -> dict[str, int]:
    """Coerce the LLM's axis values to ints in [0, 3]; missing/garbage -> 0."""
    out: dict[str, int] = {}
    for name in AXES:
        try:
            v = int(raw.get(name, 0))
        except TypeError, ValueError:
            v = 0
        out[name] = max(0, min(3, v))
    return out


def decide(axes: dict[str, int], rule: DecisionRule) -> bool:
    """Apply the transparent keep rule to one item's clamped axis scores.

    Keep iff the item is credible enough, not redundant, and at least one
    carrier axis (magnitude / dissonance / relevance) clears the floor. The
    credibility gate is what stops a high `dissonance` score from carrying an
    unconfirmed rumor.
    """
    if axes.get("credibility", 0) < rule.min_credibility:
        return False
    if axes.get("redundancy", 0) >= rule.redundancy_veto:
        return False
    return max(axes.get(a, 0) for a in CARRIERS) >= rule.keep_floor


def rule_from_cfg(curate_cfg: dict) -> DecisionRule | None:
    """Build a DecisionRule from `curate.decision`, or None when scoring is off."""
    cfg = curate_cfg.get("decision")
    if not isinstance(cfg, dict) or cfg.get("mode", "llm") != "scored":
        return None
    return DecisionRule(
        keep_floor=int(cfg.get("keep_floor", 2)),
        redundancy_veto=int(cfg.get("redundancy_veto", 2)),
        min_credibility=int(cfg.get("min_credibility", 1)),
    )
