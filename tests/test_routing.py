"""Tests for the routing eval's scoring and caching (evals/routing.py).

The API call is stubbed, so these are free and offline like the rest of the suite. What's
worth protecting:

  - the scoring math (a metric that's subtly wrong is worse than no metric, because it
    still moves and you'd trust it),
  - `prompts.SYSTEM` going out verbatim — the eval's entire claim is that it tests the real
    policy artifact, and a paraphrase would silently void that,
  - the cache keying on the prompt hash, so editing prompts.py invalidates verdicts instead
    of serving yesterday's answers for today's policy.

Run:  .venv/bin/pytest -q
"""

import pytest

import prompts
from evals.routing import CLASSIFY_TOOL, _cache_key, classify, score
from evals.labels import LABELS


def _row(rid, headline, gold, tags=(), url=None):
    return {"id": rid, "headline": headline, "url": url or f"https://example.com/{rid}",
            "source": "TestWire", "ts": None, "tool": "get_payments_news",
            "gold_section": gold, "tags": list(tags), "reviewed": True}


def _preds(**kw):
    return {rid: {"section": sec, "reason": "test"} for rid, sec in kw.items()}


# --- scoring ------------------------------------------------------------------------------


def test_perfect_predictions_score_one():
    rows = [_row("r1", "Zelle adds fraud controls", "money_movement"),
            _row("r2", "Treasury yields climb", "liquidity"),
            _row("r3", "Gold rallies on rate bets", "drop")]
    result = score(rows, _preds(r1="money_movement", r2="liquidity", r3="drop"))

    assert result["metrics"]["accuracy"] == 1.0
    assert result["metrics"]["macro_f1"] == 1.0
    assert result["metrics"]["drop_recall"] == 1.0
    assert result["failures"] == []


def test_accuracy_and_failures_track_each_disagreement():
    rows = [_row("r1", "Zelle adds fraud controls", "money_movement"),
            _row("r2", "Treasury yields climb", "liquidity"),
            _row("r3", "Gold rallies on rate bets", "drop"),
            _row("r4", "Microsoft ships new models", "ai")]
    result = score(rows, _preds(r1="money_movement", r2="liquidity", r3="liquidity", r4="ai"))

    assert result["metrics"]["accuracy"] == 0.75
    assert len(result["failures"]) == 1
    fail = result["failures"][0]
    assert (fail["id"], fail["gold"], fail["pred"]) == ("r3", "drop", "liquidity")


def test_per_section_precision_and_recall_are_distinct():
    """Over-predicting a section must cost precision, not recall — the two failure modes
    ('misses cross-border stories' vs 'dumps everything into Money Movement') need to be
    tellable apart, since prompt edits cause one or the other."""
    rows = [_row("r1", "Zelle fraud rule", "money_movement"),
            _row("r2", "Visa interchange suit", "money_movement"),
            _row("r3", "Treasury yields climb", "liquidity")]
    # Everything predicted money_movement: perfect MM recall, poor MM precision.
    result = score(rows, _preds(r1="money_movement", r2="money_movement", r3="money_movement"))

    mm = result["breakdowns"]["by_section"]["money_movement"]
    assert mm["recall"] == 1.0
    assert mm["precision"] == pytest.approx(2 / 3, abs=1e-4)  # reports round to 4dp
    assert result["breakdowns"]["by_section"]["liquidity"]["recall"] == 0.0


def test_macro_f1_ignores_classes_with_no_gold_examples():
    """Finance has 9 labels and cbdc has none; averaging in empty classes as zeros would
    drag the headline number down for a reason that has nothing to do with the model."""
    rows = [_row("r1", "Zelle fraud rule", "money_movement")]
    result = score(rows, _preds(r1="money_movement"))

    assert result["metrics"]["macro_f1"] == 1.0
    assert result["breakdowns"]["by_section"]["finance"]["support"] == 0
    assert "recall.finance" not in result["metrics"]  # not gated when nothing supports it


def test_a_missing_verdict_counts_as_wrong_not_as_a_free_pass():
    """If the model skips an item, silently excluding it from the denominator would let a
    model score 1.0 by answering one question."""
    rows = [_row("r1", "Zelle fraud rule", "money_movement"),
            _row("r2", "Treasury yields climb", "liquidity")]
    result = score(rows, _preds(r1="money_movement"))

    assert result["metrics"]["accuracy"] == 0.5
    assert result["failures"][0]["pred"] is None


def test_by_tag_accuracy_is_scoped_to_tagged_rows():
    rows = [_row("r1", "Visa expands cross-border corridor", "money_movement", ["cross-border"]),
            _row("r2", "Remittance fees fall in Mexico", "money_movement", ["cross-border"]),
            _row("r3", "Microsoft ships new models", "ai")]
    result = score(rows, _preds(r1="money_movement", r2="finance", r3="ai"))

    tag = result["breakdowns"]["by_tag"]["cross-border"]
    assert tag["support"] == 2
    assert tag["accuracy"] == 0.5


def test_every_label_appears_in_the_confusion_matrix_keys():
    rows = [_row("r1", "Zelle fraud rule", "money_movement")]
    result = score(rows, _preds(r1="drop"))
    assert set(result["confusion"]) >= set(LABELS)
    assert result["confusion"]["money_movement"]["drop"] == 1


# --- prompt fidelity + caching -------------------------------------------------------------


class _StubClient:
    def __init__(self, verdicts):
        self.verdicts = verdicts
        self.requests = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        ids = [c["id"] for c in self.verdicts]
        block = type("B", (), {"type": "tool_use", "name": "classify_items",
                               "input": {"classifications": self.verdicts}})()
        usage = type("U", (), {"input_tokens": 100, "output_tokens": 20,
                               "cache_read_input_tokens": 0})()
        return type("R", (), {"content": [block], "usage": usage, "stop_reason": "tool_use"})()


@pytest.fixture
def stub(monkeypatch, tmp_path):
    import evals.routing as routing
    monkeypatch.setattr(routing, "CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(routing.config, "ANTHROPIC_API_KEY", "test-key")

    def _install(verdicts):
        client = _StubClient(verdicts)
        monkeypatch.setattr(routing.anthropic, "Anthropic", lambda **kw: client)
        return client
    return _install


def test_the_real_policy_is_sent_verbatim_as_the_system_prompt(stub):
    """The eval's core claim. A paraphrase here would make every number meaningless."""
    client = stub([{"id": "r1", "section": "money_movement", "reason": "p2p rail"}])
    classify([_row("r1", "Zelle fraud rule", "money_movement")], "claude-haiku-4-5", quiet=True)

    system = client.requests[0]["system"]
    assert system[0]["text"] == prompts.SYSTEM
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_the_classify_tool_is_strict_and_enumerated(stub):
    """strict + an enum is what stops the model inventing a section name that would then be
    scored as a wrong answer rather than a malformed one."""
    assert CLASSIFY_TOOL["strict"] is True
    item = CLASSIFY_TOOL["input_schema"]["properties"]["classifications"]["items"]
    assert item["additionalProperties"] is False
    assert item["properties"]["section"]["enum"] == list(LABELS)

    client = stub([{"id": "r1", "section": "ai", "reason": "policy"}])
    classify([_row("r1", "Anthropic on AI bill", "ai")], "claude-haiku-4-5", quiet=True)
    assert client.requests[0]["tool_choice"] == {"type": "tool", "name": "classify_items"}


def test_no_sampling_params_are_sent(stub):
    """temperature/top_p are rejected outright on current models; determinism comes from the
    cache instead. Sending one would 400 the moment anyone sweeps a newer model."""
    stub([{"id": "r1", "section": "ai", "reason": "x"}])
    classify([_row("r1", "Anthropic on AI bill", "ai")], "claude-haiku-4-5", quiet=True)
    import evals.routing as routing
    sent = routing.anthropic.Anthropic().requests[0]
    assert "temperature" not in sent and "top_p" not in sent and "top_k" not in sent


def test_a_second_run_is_served_from_cache(stub):
    rows = [_row("r1", "Zelle fraud rule", "money_movement")]
    client = stub([{"id": "r1", "section": "money_movement", "reason": "p2p"}])

    _, first = classify(rows, "claude-haiku-4-5", quiet=True)
    _, second = classify(rows, "claude-haiku-4-5", quiet=True)

    assert first["requests"] == 1 and first["cache_hits"] == 0
    assert second["requests"] == 0 and second["cache_hits"] == 1
    assert len(client.requests) == 1


def test_editing_the_policy_invalidates_cached_verdicts(monkeypatch):
    """The point of keying on the prompt hash: a policy edit must not be answered with
    verdicts formed under the old policy."""
    row = _row("r1", "Zelle fraud rule", "money_movement")
    before = _cache_key("claude-haiku-4-5", row)
    monkeypatch.setattr(prompts, "SYSTEM", prompts.SYSTEM + "\n- new tier rule")
    assert _cache_key("claude-haiku-4-5", row) != before


def test_the_model_under_test_is_part_of_the_cache_key():
    row = _row("r1", "Zelle fraud rule", "money_movement")
    assert _cache_key("claude-haiku-4-5", row) != _cache_key("claude-sonnet-4-6", row)
