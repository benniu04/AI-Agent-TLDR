"""Tests for the routing-dataset builder (evals/label.py).

The properties worth protecting here are all about not corrupting gold labels: bootstrap
must never overwrite something you reviewed, must never re-add an item you already have, and
must never mark its own guess as reviewed. A bug in any of those quietly turns the eval into
a machine grading its own homework.

Run:  .venv/bin/pytest -q
"""

import json

import pytest

from evals.label import _TOOL_PRIOR, _auto_tags, cmd_bootstrap, load_dataset, save_dataset


class _Args:
    def __init__(self, dataset, **kw):
        self.dataset = dataset
        self.pool = kw.get("pool", [])
        self.include_memory = kw.get("include_memory", False)


@pytest.fixture
def pool(tmp_path, monkeypatch):
    """A one-tool fixture pool on disk, wired in via FIXTURES_DIR."""
    import evals.label as label_mod

    fixtures = tmp_path / "fixtures"
    (fixtures / "2026-09-03").mkdir(parents=True)
    bundle = {"schema": 1, "date": "2026-09-03", "tools": {"get_payments_news": {
        "is_error": False,
        "content": json.dumps([
            {"title": "Zelle adds scam reimbursement for elder fraud",
             "url": "https://example.com/zelle-scam", "source": "PYMNTS", "ts": 1_780_000_000},
            {"title": "Google brings conversational AI search to Gmail",
             "url": "https://example.com/google-gmail-ai", "source": "PYMNTS", "ts": 1_780_000_001},
        ])}}}
    (fixtures / "2026-09-03" / "pool.json").write_text(json.dumps(bundle))
    monkeypatch.setattr(label_mod, "FIXTURES_DIR", str(fixtures))
    return str(fixtures)


def test_bootstrap_seeds_rows_as_unreviewed_guesses(pool, tmp_path):
    ds = str(tmp_path / "routing.jsonl")
    cmd_bootstrap(_Args(ds, pool=["2026-09-03"]))
    rows = load_dataset(ds)

    assert len(rows) == 2
    for r in rows:
        assert r["reviewed"] is False, "a bootstrapped guess must never count as gold"
        assert r["label_source"] == "bootstrap"
        assert r["gold_section"] == "money_movement"  # the feed's prior, right or wrong


def test_bootstrap_is_idempotent(pool, tmp_path):
    """Re-running after a new capture must not duplicate what's already there."""
    ds = str(tmp_path / "routing.jsonl")
    cmd_bootstrap(_Args(ds, pool=["2026-09-03"]))
    cmd_bootstrap(_Args(ds, pool=["2026-09-03"]))
    assert len(load_dataset(ds)) == 2


def test_bootstrap_never_clobbers_a_reviewed_label(pool, tmp_path):
    """The whole dataset's value is the human calls in it. Re-running capture+bootstrap
    tomorrow must not reset a section you corrected today."""
    ds = str(tmp_path / "routing.jsonl")
    cmd_bootstrap(_Args(ds, pool=["2026-09-03"]))

    rows = load_dataset(ds)
    target = next(r for r in rows if "gmail" in r["url"])
    target.update(gold_section="ai", reviewed=True, label_source="human")
    save_dataset(rows, ds)

    cmd_bootstrap(_Args(ds, pool=["2026-09-03"]))

    after = next(r for r in load_dataset(ds) if "gmail" in r["url"])
    assert after["gold_section"] == "ai"
    assert after["reviewed"] is True
    assert after["label_source"] == "human"


def test_ids_stay_unique_as_the_set_grows(pool, tmp_path):
    ds = str(tmp_path / "routing.jsonl")
    cmd_bootstrap(_Args(ds, pool=["2026-09-03"]))
    rows = load_dataset(ds)
    rows.append({**rows[0], "id": "r0099", "url": "https://example.com/other"})
    save_dataset(rows, ds)
    cmd_bootstrap(_Args(ds, pool=["2026-09-03"]))

    ids = [r["id"] for r in load_dataset(ds)]
    assert len(ids) == len(set(ids))


def test_every_source_tool_has_a_prior():
    """A tool with no prior silently yields rows with gold_section=None, which read as a
    labeling gap rather than the missing mapping they actually are."""
    import tools
    for name in tools.CLIENT_TOOLS:
        assert name in _TOOL_PRIOR, f"{name} has no bootstrap prior"


@pytest.mark.parametrize("headline,expected", [
    ("Visa expands cross-border remittance corridor to Mexico", "cross-border"),
    ("ECB advances digital euro pilot with three banks", "cbdc"),
    ("Gold slips as rate-hike fears build", "commodity"),
    ("Revolut gets conditional OCC nod for US charter", "foreign-neobank"),
    ("Zelle adds scam reimbursement for elder fraud", "p2p"),
    ("Ransomware crew breaches regional bank vendor", "breach"),
    ("Fintech raises $40M Series B for expense cards", "vc-round"),
])
def test_auto_tags_find_the_contested_buckets(headline, expected):
    assert expected in _auto_tags(headline)


def test_auto_tags_are_empty_for_plain_news():
    assert _auto_tags("Microsoft unveils new reasoning models at Build") == []


# --- disputed-audit mode -------------------------------------------------------------------


def _report(tmp_path, model, failures):
    import json as _json
    d = tmp_path / "reports"
    d.mkdir(exist_ok=True)
    p = d / "20260101T000000Z-routing.json"
    p.write_text(_json.dumps({"suite": "routing", "subject": {"model": model},
                              "failures": failures}))
    return str(p)


def test_load_disputes_maps_failures_by_row_id(tmp_path):
    from evals.label import _load_disputes
    path = _report(tmp_path, "claude-sonnet-4-6",
                   [{"id": "r0001", "gold": "finance", "pred": "drop", "reason": "market recap"}])

    disputes, judged_by = _load_disputes(path)

    assert disputes == {"r0001": {"pred": "drop", "reason": "market recap"}}
    assert "claude-sonnet-4-6" in judged_by


def test_audit_flips_a_label_and_records_what_changed(pool, tmp_path, monkeypatch):
    """The audit's value is traceability: a corrected label must say it was corrected, and
    against which model — otherwise a later reader can't tell a considered flip from a typo."""
    import evals.label as label_mod
    ds = str(tmp_path / "routing.jsonl")
    cmd_bootstrap(_Args(ds, pool=["2026-09-03"]))

    rows = load_dataset(ds)
    rows[0].update(gold_section="finance", reviewed=True, label_source="human")
    save_dataset(rows, ds)
    rid = rows[0]["id"]

    path = _report(tmp_path, "claude-sonnet-4-6",
                   [{"id": rid, "gold": "finance", "pred": "drop", "reason": "market recap"}])
    keys = iter(["d", "q"])  # accept the model's argument, then quit
    monkeypatch.setattr(label_mod, "_getch", lambda: next(keys))

    class A:
        dataset, disputed, tag, tool, limit, all = ds, path, None, None, None, False
    label_mod.cmd_review(A())

    after = next(r for r in load_dataset(ds) if r["id"] == rid)
    assert after["gold_section"] == "drop"
    assert "was finance" in after["notes"] and "claude-sonnet-4-6" in after["notes"]


def test_audit_keeps_your_label_when_you_stand_by_it(pool, tmp_path, monkeypatch):
    import evals.label as label_mod
    ds = str(tmp_path / "routing.jsonl")
    cmd_bootstrap(_Args(ds, pool=["2026-09-03"]))
    rows = load_dataset(ds)
    rows[0].update(gold_section="finance", reviewed=True, label_source="human")
    save_dataset(rows, ds)
    rid = rows[0]["id"]

    path = _report(tmp_path, "claude-haiku-4-5",
                   [{"id": rid, "gold": "finance", "pred": "drop", "reason": "recap"}])
    keys = iter(["k", "q"])
    monkeypatch.setattr(label_mod, "_getch", lambda: next(keys))

    class A:
        dataset, disputed, tag, tool, limit, all = ds, path, None, None, None, False
    label_mod.cmd_review(A())

    after = next(r for r in load_dataset(ds) if r["id"] == rid)
    assert after["gold_section"] == "finance"
    assert "upheld" in after["notes"]


def test_audit_queue_contains_only_disputed_rows(pool, tmp_path, monkeypatch):
    """Undisputed rows must not leak into the audit — the point is to spend your attention
    only where the model and the labels actually conflict."""
    import evals.label as label_mod
    ds = str(tmp_path / "routing.jsonl")
    cmd_bootstrap(_Args(ds, pool=["2026-09-03"]))
    rows = load_dataset(ds)
    for r in rows:
        r.update(gold_section="money_movement", reviewed=True, label_source="human")
    save_dataset(rows, ds)

    path = _report(tmp_path, "claude-haiku-4-5",
                   [{"id": rows[0]["id"], "gold": "money_movement", "pred": "ai", "reason": "x"}])
    seen = []
    monkeypatch.setattr(label_mod, "_getch", lambda: (seen.append(1), "q")[1])

    class A:
        dataset, disputed, tag, tool, limit, all = ds, path, None, None, None, False
    label_mod.cmd_review(A())

    assert len(seen) == 1  # prompted for exactly the one disputed row before quitting
