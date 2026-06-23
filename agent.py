"""The hand-written agent loop.

This is the whole point of the project: a goal + tools, run in a loop where the MODEL
decides which tool to call and when it's done. Our code just executes whatever it picks
and feeds the result back.

Server tools (web_search/web_fetch) are executed by Anthropic inside a single turn and
show up as `server_tool_use` blocks — we never run them. We only dispatch our custom
client tools (`tool_use` blocks). The loop handles three stop reasons:
  - tool_use   -> run the client tool(s), append tool_result(s), continue
  - pause_turn -> a server tool is mid-flight, resend the assistant content, continue
  - end_turn   -> done; return the joined text

Everything is bounded (iterations, cumulative tokens, wall clock) and every step is
logged to a JSONL trajectory so the run is debuggable.
"""

import json
import os
import re
import time

import anthropic

import config
from formatting import canonical_url
from tools import TOOLS, dispatch

# How much of a tool result we keep in the log (full result still goes to the model).
_LOG_RESULT_CHARS = 500

# Extract URLs from client tool results (e.g. get_hacker_news) for the provenance allowlist.
_URL_RE = re.compile(r"https?://[^\s\"'<>)\]]+")


def _collect_dates(content: str, into: dict) -> None:
    """Map canonical_url -> epoch publish time from a client tool's JSON result.

    Source tools (RSS/finance/HN) include a `ts` field per item; capturing it lets the
    recency filter deterministically drop stale stories. Best-effort: ignore bad JSON.
    """
    try:
        items = json.loads(content)
    except (ValueError, TypeError):
        return
    if not isinstance(items, list):
        return
    for it in items:
        if isinstance(it, dict) and it.get("url") and it.get("ts"):
            try:
                into[canonical_url(it["url"])] = int(it["ts"])
            except (ValueError, TypeError):
                continue  # malformed ts — skip rather than crash the run

# Transient-API handling: a run can hit a per-minute token cap (429) OR a server-side
# overload/error (529/500/502/503) — and the network can blip. All are transient, so retry
# with bounded backoff honoring Retry-After when present. Non-transient errors (400/401/404)
# re-raise immediately. Without this, a single 529 "Overloaded" kills the whole daily run.
_API_RETRIES = 4
_RETRY_BASE_WAIT = 20  # seconds base; backs off as base*(attempt+1) when no Retry-After header
_RETRYABLE_STATUS = {429, 500, 502, 503, 529}


def _create_with_retry(client, log, **kwargs):
    for attempt in range(_API_RETRIES + 1):
        try:
            return client.messages.create(**kwargs)
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
            status = getattr(exc, "status_code", None)  # None for connection errors
            transient = status is None or status in _RETRYABLE_STATUS
            if attempt == _API_RETRIES or not transient:
                raise  # out of retries, or a non-transient error (400/401/404) — don't loop
            hdrs = getattr(getattr(exc, "response", None), "headers", {}) or {}
            wait = int(hdrs.get("retry-after", _RETRY_BASE_WAIT * (attempt + 1)))
            log({"event": "api_retry", "status": status, "type": type(exc).__name__,
                 "attempt": attempt, "wait_seconds": wait})
            time.sleep(wait)


class AgentResult:
    def __init__(self, text: str, stop: str, iterations: int, tokens: int, log_path: str,
                 data: dict | None = None, seen_urls: set | None = None,
                 url_ts: dict | None = None):
        self.text = text
        self.data = data  # structured briefing captured from the submit_tldr tool, if any
        self.seen_urls = seen_urls or set()  # canonical URLs search actually returned
        self.url_ts = url_ts or {}  # canonical URL -> epoch publish time (for recency)
        self.stop = stop  # end_turn | submitted | max_iterations | budget | timeout
        self.iterations = iterations
        self.tokens = tokens
        self.log_path = log_path


def _apply_cache_breakpoint(messages: list) -> None:
    """Keep ONE rolling prompt-cache breakpoint on the most recent user message we built.

    Combined with the breakpoint on system+tools, this caches the whole conversation prefix up
    to the latest user turn — so re-sent web_search/feed history is billed as cheap cache reads
    (~10%) instead of full input on every turn. Only touches dict content blocks we construct;
    SDK assistant blocks are left untouched. Stays at 2 breakpoints total (under the 4 limit).
    """
    for m in messages:  # clear any previous rolling breakpoint
        content = m.get("content")
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    blk.pop("cache_control", None)
    for m in reversed(messages):  # set it on the latest user message with markable content
        if m.get("role") == "user":
            content = m.get("content")
            if isinstance(content, list) and content and isinstance(content[-1], dict):
                content[-1]["cache_control"] = {"type": "ephemeral"}
            break


def _final_text(content) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


def _collect_result_urls(content, into: set) -> None:
    """Add the canonical URL of every web_search/web_fetch result in `content` to `into`.

    This is the provenance allowlist: the set of URLs search actually returned this run.
    Output URLs not in this set were fabricated by the model. No cap — collect them all.
    """
    for block in content or []:
        if getattr(block, "type", None) not in ("web_search_tool_result", "web_fetch_tool_result"):
            continue
        results = getattr(block, "content", None)
        if not isinstance(results, list):
            continue  # error results (e.g. max_uses_exceeded) aren't a list of results
        for r in results:
            url = getattr(r, "url", None)
            if url:
                into.add(canonical_url(url))


def _block_to_jsonable(block):
    """Best-effort compact representation of a content block for the trajectory log."""
    btype = getattr(block, "type", "?")
    if btype == "text":
        return {"type": "text", "text": block.text[:_LOG_RESULT_CHARS]}
    if btype == "tool_use":
        return {"type": "tool_use", "name": block.name, "input": block.input, "id": block.id}
    if btype == "server_tool_use":
        return {"type": "server_tool_use", "name": block.name, "input": getattr(block, "input", None)}
    if btype in ("web_search_tool_result", "web_fetch_tool_result"):
        out = {"type": btype, "tool_use_id": getattr(block, "tool_use_id", None)}
        # Capture the URLs/titles the search returned, so we can see what the model had to
        # work with (and later validate submitted URLs against real results).
        content = getattr(block, "content", None)
        if isinstance(content, list):
            results = []
            for r in content[:10]:
                url = getattr(r, "url", None)
                if url:
                    results.append({"url": url, "title": (getattr(r, "title", "") or "")[:90]})
            if results:
                out["results"] = results
        return out
    return {"type": btype}


def run_agent(
    goal: str,
    system: str,
    tools: list | None = None,
    *,
    log_dir: str = "logs",
    run_id: str | None = None,
) -> AgentResult:
    """Run the agent loop to completion and return the final text + run metadata."""
    config.require_anthropic_key()
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    tools = TOOLS if tools is None else tools

    os.makedirs(log_dir, exist_ok=True)
    run_id = run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    log_path = os.path.join(log_dir, f"{run_id}.jsonl")

    def log(event: dict):
        event["t"] = time.time()
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")

    # Goal in block form so it can carry a cache_control breakpoint (see _apply_cache_breakpoint).
    messages = [{"role": "user", "content": [{"type": "text", "text": goal}]}]
    # Cache the static prefix (tools + system) — identical every call, so it's read from cache
    # after the first turn instead of re-sent at full price.
    cached_system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    started = time.monotonic()
    total_tokens = 0
    stop = "max_iterations"
    final_data = None
    nudged = False     # whether we've sent the soft-deadline "wrap up now" nudge yet
    seen_urls = set()  # canonical URLs search actually returned (provenance allowlist)
    url_ts = {}        # canonical URL -> epoch publish time (for recency filtering)

    log({"event": "run_start", "model": config.MODEL, "goal": goal,
         "bounds": {"max_iterations": config.MAX_ITERATIONS,
                    "max_tokens_budget": config.MAX_TOKENS_BUDGET,
                    "wall_clock_seconds": config.WALL_CLOCK_SECONDS}})

    for i in range(config.MAX_ITERATIONS):
        # --- Bounds checks before each (paid) call ---
        elapsed = time.monotonic() - started
        if elapsed > config.WALL_CLOCK_SECONDS:
            stop = "timeout"
            break
        if total_tokens >= config.MAX_TOKENS_BUDGET:
            stop = "budget"
            break

        _apply_cache_breakpoint(messages)  # roll the conversation cache breakpoint forward
        resp = _create_with_retry(
            client,
            log,
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS_PER_CALL,
            system=cached_system,
            tools=tools,
            messages=messages,
        )

        usage = resp.usage
        total_tokens += (usage.input_tokens or 0) + (usage.output_tokens or 0)
        server_searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", None)
        log({"event": "step", "i": i, "stop_reason": resp.stop_reason,
             "content": [_block_to_jsonable(b) for b in resp.content],
             "cumulative_tokens": total_tokens, "server_searches": server_searches,
             "cache_read": getattr(usage, "cache_read_input_tokens", None),
             "cache_creation": getattr(usage, "cache_creation_input_tokens", None)})

        messages.append({"role": "assistant", "content": resp.content})
        _collect_result_urls(resp.content, seen_urls)  # grow the provenance allowlist

        if resp.stop_reason == "end_turn":
            stop = "end_turn"
            break

        if resp.stop_reason == "pause_turn":
            # A server tool is still running; resend the partial turn unchanged.
            continue

        if resp.stop_reason == "tool_use":
            # Terminal case: the agent submitted the finished briefing. Capture it and stop.
            submit = next((b for b in resp.content
                           if getattr(b, "type", None) == "tool_use" and b.name == "submit_tldr"),
                          None)
            if submit is not None:
                final_data = submit.input
                stop = "submitted"
                log({"event": "submitted", "i": i,
                     "sections": [s.get("name") for s in final_data.get("sections", [])]})
                break

            tool_results = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue  # text + server_tool_use blocks are not ours to run
                content, is_error = dispatch(block.name, block.input)
                if not is_error:
                    # Client tools return URLs (provenance) and publish timestamps (recency).
                    for m in _URL_RE.findall(content):
                        seen_urls.add(canonical_url(m))
                    _collect_dates(content, url_ts)
                log({"event": "tool_call", "i": i, "name": block.name,
                     "input": block.input, "is_error": is_error,
                     "result_preview": content[:_LOG_RESULT_CHARS]})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                })
            # Soft deadline: if we're low on wall-clock, tell the agent (once) to wrap up now,
            # so a slow run submits a partial digest instead of timing out with nothing. Safe to
            # piggyback on this tool_result user message (valid: tool_result blocks + a text block).
            if not nudged and time.monotonic() - started > config.WALL_CLOCK_SECONDS * 0.85:
                tool_results.append({"type": "text", "text":
                    "You are almost out of time. Stop gathering and call the submit_tldr tool "
                    "NOW with the best briefing you have so far — do not run more tools or write "
                    "commentary first."})
                nudged = True
                log({"event": "soft_deadline_nudge", "i": i})
            messages.append({"role": "user", "content": tool_results})
            continue

        if resp.stop_reason == "max_tokens":
            # The turn was cut off (often: model narrated heavily while gathering and ran
            # out of output room before calling submit_tldr). It already has the search
            # results in context, so nudge it to just submit. Bounded by MAX_ITERATIONS.
            log({"event": "max_tokens_recover", "i": i})
            # The API requires a tool_result for every (client) tool_use in the turn —
            # the truncation may have left one dangling, so satisfy those first.
            recovery = [{"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                         "content": "Previous turn was cut off; ignore and resubmit."}
                        for b in resp.content if getattr(b, "type", None) == "tool_use"]
            recovery.append({"type": "text", "text":
                "Your previous message was cut off before you finished. You already have all "
                "the information you need. Call the submit_tldr tool now with the final "
                "briefing — do not write any analysis or commentary first."})
            messages.append({"role": "user", "content": recovery})
            continue

        # Anything unexpected: stop cleanly rather than loop blindly.
        stop = resp.stop_reason or "unknown"
        break

    text = _final_text(messages[-1]["content"]) if messages[-1]["role"] == "assistant" else ""
    log({"event": "run_end", "stop": stop, "iterations": i + 1,
         "total_tokens": total_tokens, "final_text_chars": len(text),
         "submitted": final_data is not None, "result_urls_seen": len(seen_urls)})

    return AgentResult(text=text, stop=stop, iterations=i + 1, tokens=total_tokens,
                       log_path=log_path, data=final_data, seen_urls=seen_urls, url_ts=url_ts)


if __name__ == "__main__":
    # Phase 1 smoke test: one server tool, a toy goal, prove the loop self-terminates.
    toy_tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
    result = run_agent(
        goal="What's one notable tech headline from the last day? One sentence.",
        system="You are a terse assistant. Search if needed, then answer in one sentence.",
        tools=toy_tools,
    )
    print(f"\n--- stop={result.stop} iters={result.iterations} tokens={result.tokens} ---")
    print(result.text)
    print(f"(trajectory: {result.log_path})")
