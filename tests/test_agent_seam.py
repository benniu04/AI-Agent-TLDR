"""Tests for the injectable tool dispatcher in agent.run_agent (agent.py).

This is the seam the replay eval runs through: a fixture-backed dispatcher plus a tool list
with the server tools removed, so the model does real editorial work on a frozen source pool.
Two properties have to hold for that to measure anything, and both are easy to break by
accident:

  1. the injected dispatcher is actually the one that runs, and
  2. provenance and publish timestamps still get collected — offline they come from the
     dispatched result string (agent.py's _URL_RE / _collect_dates), not from the
     server-tool result blocks, which never appear when web_search isn't declared.

The Anthropic client is stubbed, so this is free and offline like the rest of the suite.

Run:  .venv/bin/pytest -q
"""

import json

import pytest

import agent
import tools as tools_mod


class _Block:
    """Stands in for an SDK content block (SimpleNamespace won't do — the loop reads .type
    via getattr and the SDK's blocks are attribute objects, not dicts)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Usage:
    input_tokens = 10
    output_tokens = 5
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0
    server_tool_use = None


class _Response:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Usage()


class _StubClient:
    """Returns a scripted sequence of responses: first a client-tool call, then submit_tldr."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


SUBMITTED = {"date": "Fri, Jun 5", "sections": [
    {"name": "Tech", "items": [{"headline": "Cloudflare acquires VoidZero tooling",
                                "url": "https://example.com/cloudflare-voidzero"}]}]}


@pytest.fixture
def stubbed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(agent.config, "ANTHROPIC_API_KEY", "test-key")
    responses = [
        _Response([_Block(type="tool_use", id="t1", name="get_tech_news", input={"limit": 5})],
                  "tool_use"),
        _Response([_Block(type="tool_use", id="t2", name="submit_tldr", input=SUBMITTED)],
                  "tool_use"),
    ]
    client = _StubClient(responses)
    monkeypatch.setattr(agent.anthropic, "Anthropic", lambda **kw: client)
    return client


def _run(tmp_path, dispatch_fn=None, tools=None):
    return agent.run_agent(goal="assemble the briefing", system="be an editor",
                           tools=tools, log_dir=str(tmp_path), run_id="test",
                           dispatch_fn=dispatch_fn)


def test_injected_dispatcher_replaces_the_real_one(stubbed, tmp_path, monkeypatch):
    """If the real dispatch were still wired in, this would hit the network."""
    monkeypatch.setattr(tools_mod, "dispatch",
                        lambda *a, **k: pytest.fail("real dispatch was called"))
    seen = []

    def fake_dispatch(name, tool_input):
        seen.append((name, tool_input))
        return json.dumps([{"title": "x", "url": "https://example.com/x"}]), False

    result = _run(tmp_path, dispatch_fn=fake_dispatch)
    assert seen == [("get_tech_news", {"limit": 5})]
    assert result.stop == "submitted"
    assert result.data == SUBMITTED


def test_omitting_dispatch_fn_still_uses_the_real_dispatcher(stubbed, tmp_path, monkeypatch):
    """The production path must be untouched by the new parameter."""
    called = []
    monkeypatch.setattr(agent, "dispatch",
                        lambda name, ti: (called.append(name), ("[]", False))[1])
    _run(tmp_path)
    assert called == ["get_tech_news"]


def test_provenance_and_timestamps_come_from_the_dispatched_result(stubbed, tmp_path):
    """Offline there are no web_search result blocks, so _collect_result_urls is a no-op and
    the allowlist has to be built by scraping the tool result — which is what makes the
    replay eval's link_provenance check meaningful."""
    pool = [
        {"title": "Cloudflare acquires VoidZero", "url": "https://example.com/cloudflare-voidzero",
         "ts": 1_780_000_000},
        {"title": "Some other story", "url": "https://example.com/other", "ts": 1_779_000_000},
    ]

    result = _run(tmp_path, dispatch_fn=lambda n, ti: (json.dumps(pool), False))

    assert "example.com/cloudflare-voidzero" in result.seen_urls
    assert "example.com/other" in result.seen_urls
    assert result.url_ts["example.com/cloudflare-voidzero"] == 1_780_000_000


def test_a_failing_fixture_tool_is_returned_as_data_not_raised(stubbed, tmp_path):
    """dispatch's contract is (content, is_error) — a fixture dispatcher must be able to
    report a missing tool the same way, without killing the run."""
    result = _run(tmp_path, dispatch_fn=lambda n, ti: (f"Unknown tool: {n}", True))
    assert result.stop == "submitted"


def test_offline_tool_list_drops_server_tools_but_keeps_the_rest():
    """How the replay eval suppresses web_search/web_fetch: the model can't emit a tool_use
    for a tool that isn't declared, so filtering the list is sufficient — no prod change."""
    offline = [t for t in tools_mod.TOOLS if t.get("name") not in {"web_search", "web_fetch"}]

    names = {t["name"] for t in offline}
    assert "web_search" not in names and "web_fetch" not in names
    assert "submit_tldr" in names
    assert set(tools_mod.CLIENT_TOOLS) <= names
    assert not any("type" in t for t in offline)  # server tools are the ones carrying `type`
