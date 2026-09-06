"""Eval harness for the Daily TLDR agent.

The unit tests in tests/ cover the deterministic filter chain — the part that runs after
the model has already decided. This package covers the part the tests can't: the editorial
judgment encoded in prompts.py (which section a story belongs to, what's worth including),
which is where nearly all the iteration happens and where nothing currently measures
whether an edit helped or hurt.

Suites, cheapest first:
  digest   deterministic graders over a recorded run    free, no API key, runs in CI
  routing  prompts.py's section policy vs labeled data  ~$0.04
  replay   the real agent loop on a frozen source pool  ~$0.30

Nothing in here is imported by the production path; pytest.ini scopes collection to tests/,
so this package stays out of the free test suite.
"""
