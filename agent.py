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
import time

import anthropic

import config
from tools import TOOLS, dispatch

# How much of a tool result we keep in the log (full result still goes to the model).
_LOG_RESULT_CHARS = 500

# Rate-limit handling: a single run pulls a lot of search-result tokens, which can hit
# a per-minute input-token cap. Retry on 429 honoring Retry-After, with bounded backoff.
_RATE_LIMIT_RETRIES = 4
_RATE_LIMIT_DEFAULT_WAIT = 20  # seconds, if no Retry-After header is provided


def _create_with_retry(client, log, **kwargs):
    for attempt in range(_RATE_LIMIT_RETRIES + 1):
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            if attempt == _RATE_LIMIT_RETRIES:
                raise
            retry_after = getattr(getattr(exc, "response", None), "headers", {}) or {}
            wait = int(retry_after.get("retry-after", _RATE_LIMIT_DEFAULT_WAIT))
            log({"event": "rate_limited", "attempt": attempt, "wait_seconds": wait})
            time.sleep(wait)


class AgentResult:
    def __init__(self, text: str, stop: str, iterations: int, tokens: int, log_path: str,
                 data: dict | None = None):
        self.text = text
        self.data = data  # structured briefing captured from the submit_tldr tool, if any
        self.stop = stop  # end_turn | submitted | max_iterations | budget | timeout
        self.iterations = iterations
        self.tokens = tokens
        self.log_path = log_path


def _final_text(content) -> str:
    return "\n".join(b.text for b in content if getattr(b, "type", None) == "text").strip()


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
        return {"type": btype, "tool_use_id": getattr(block, "tool_use_id", None)}
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

    messages = [{"role": "user", "content": goal}]
    started = time.monotonic()
    total_tokens = 0
    stop = "max_iterations"
    final_data = None

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

        resp = _create_with_retry(
            client,
            log,
            model=config.MODEL,
            max_tokens=config.MAX_TOKENS_PER_CALL,
            system=system,
            tools=tools,
            messages=messages,
        )

        usage = resp.usage
        total_tokens += (usage.input_tokens or 0) + (usage.output_tokens or 0)
        server_searches = getattr(getattr(usage, "server_tool_use", None), "web_search_requests", None)
        log({"event": "step", "i": i, "stop_reason": resp.stop_reason,
             "content": [_block_to_jsonable(b) for b in resp.content],
             "cumulative_tokens": total_tokens, "server_searches": server_searches})

        messages.append({"role": "assistant", "content": resp.content})

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
                log({"event": "tool_call", "i": i, "name": block.name,
                     "input": block.input, "is_error": is_error,
                     "result_preview": content[:_LOG_RESULT_CHARS]})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": content,
                    "is_error": is_error,
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        # max_tokens or anything unexpected: stop cleanly rather than loop blindly.
        stop = resp.stop_reason or "unknown"
        break

    text = _final_text(messages[-1]["content"]) if messages[-1]["role"] == "assistant" else ""
    log({"event": "run_end", "stop": stop, "iterations": i + 1,
         "total_tokens": total_tokens, "final_text_chars": len(text),
         "submitted": final_data is not None})

    return AgentResult(text=text, stop=stop, iterations=i + 1,
                       tokens=total_tokens, log_path=log_path, data=final_data)


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
