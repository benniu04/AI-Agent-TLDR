"""The eval label space: the five sections, plus DROP.

Production spells the section names in four places — prompts.py prose, the submit_tldr
schema description (tools.py), _SECTION_EMOJI (formatting.py), and the caps dict (run.py).
That duplication is deliberate: most of it is prose, and _SECTION_EMOJI's extra "technology"
alias exists because the model sometimes submits the long form. Refactoring a working system
onto a shared constant would buy nothing.

So instead of a refactor, this module owns ONE canonical spelling for the evals, and
tests/test_graders.py asserts it still agrees with all three production spellings. Rename a
section anywhere and CI goes red immediately, for free.

Slugs (lowercase, underscored) are what datasets and metric keys use, so a metric name like
"recall.money_movement" is stable and shell-friendly. Display names are what the model sees
and submits.
"""

# Canonical order — the order the digest must be assembled in.
SECTIONS = ("finance", "money_movement", "liquidity", "ai", "tech")

# The sixth label: the policy says to exclude this item entirely. Not a section, but the
# routing eval has to score it, and it's the class most likely to regress when a tier is
# loosened — a prompt edit that stops dropping junk shows up here first.
DROP = "drop"

LABELS = SECTIONS + (DROP,)

_DISPLAY = {
    "finance": "Finance",
    "money_movement": "Money Movement",
    "liquidity": "Liquidity",
    "ai": "AI",
    "tech": "Tech",
    DROP: "DROP",
}

# Section names the agent has actually submitted, mapped back to a slug. "Technology" is the
# long form the model reaches for sometimes (formatting.py carries the same alias in its
# emoji table, which is why an off-spelling still gets rendered rather than dropped).
_ALIASES = {
    "technology": "tech",
    "money movement": "money_movement",
    "moneymovement": "money_movement",
}


def display(label: str) -> str:
    """Slug -> the human/model-facing name ('money_movement' -> 'Money Movement')."""
    return _DISPLAY[label]


def slug(name: str) -> str | None:
    """Model-submitted section name -> slug. None if it isn't one of ours.

    Tolerant on purpose: the model's spelling is not something we control, and a caller
    grading a digest needs to distinguish "spelled it differently" from "invented a section".
    """
    key = " ".join((name or "").strip().lower().split())
    if key in _ALIASES:
        return _ALIASES[key]
    key = key.replace(" ", "_")
    return key if key in LABELS else None
