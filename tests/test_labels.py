"""Guards the eval label space against production drift (evals/labels.py).

The five section names are spelled in four production places on purpose (see the module
docstring in evals/labels.py for why we don't refactor that away). The cost of leaving the
duplication is that a rename can land in three of the four and silently break the fourth —
a section whose name no longer matches just vanishes at formatting.py's `_iter_sections`,
with no error.

These tests are that missing error. They're free, keyless, and fast, so they run on every
push. All pure imports: no network, no API keys.

Run:  .venv/bin/pytest -q
"""

import json

import pytest

import formatting
import prompts
import tools
from evals.labels import DROP, LABELS, SECTIONS, display, slug


def _submit_tldr_schema_json() -> str:
    """The submit_tldr tool declaration, serialized — section names live in its prose
    descriptions, not in an enum, so a substring check is the honest way to look for them."""
    decl = next(t for t in tools.TOOLS if t.get("name") == "submit_tldr")
    return json.dumps(decl)


@pytest.mark.parametrize("section", SECTIONS)
def test_section_name_present_in_every_production_spelling(section):
    name = display(section)
    schema = _submit_tldr_schema_json()

    # formatting.py renders the digest by looking the section name up in this table.
    assert name.lower() in formatting._SECTION_EMOJI, f"{name!r} has no emoji in formatting.py"

    # tools.py tells the model which sections to submit, in the schema description.
    assert name in schema, f"{name!r} missing from the submit_tldr schema description"

    # prompts.py names the beats in prose, mostly shouted (FINANCE, MONEY MOVEMENT, ...).
    assert name in prompts.SYSTEM or name.upper() in prompts.SYSTEM, \
        f"{name!r} missing from prompts.SYSTEM"


def test_section_order_matches_the_schema_description():
    """SECTIONS is the canonical order; the schema tells the model the same order. If someone
    reorders one and not the other, the digest ships in an order the evals don't expect."""
    schema = _submit_tldr_schema_json()
    positions = [schema.index(display(s)) for s in SECTIONS]
    assert positions == sorted(positions), (
        f"SECTIONS order {SECTIONS} disagrees with the submit_tldr schema description")


def test_run_py_cap_override_keys_resolve():
    """run.py keys its per-section cap overrides by lowercased display name (a fourth
    spelling). Those keys must still map back to real sections."""
    for key in ("money movement", "liquidity"):
        assert slug(key) in SECTIONS, f"run.py cap key {key!r} no longer names a section"


def test_slug_accepts_the_spellings_the_model_actually_submits():
    assert slug("Money Movement") == "money_movement"
    assert slug("  money   movement ") == "money_movement"  # stray whitespace
    assert slug("Technology") == "tech"  # long form; formatting.py carries the same alias
    assert slug("Tech") == "tech"
    assert slug("AI") == "ai"


def test_slug_rejects_invented_sections():
    assert slug("Crypto") is None
    assert slug("") is None
    assert slug(None) is None


def test_display_round_trips_every_label():
    for label in LABELS:
        assert slug(display(label)) == label


def test_drop_is_not_a_section():
    """DROP is a routing verdict, not a place to put a story — keeping it out of SECTIONS is
    what stops it being graded as one, or rendered into a digest."""
    assert DROP not in SECTIONS
    assert DROP in LABELS
