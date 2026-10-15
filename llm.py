"""
Ollama LLM client for the Socratic tutor.

Streaming, JSON extraction, retries. No learner-state logic.
Imported by tutor.py (re-exported for backward compatibility).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config (single source; tutor re-exports these names)
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
METRICS_DIR = BASE_DIR / "metrics"

OLLAMA_HOST = "http://localhost:11434"
MODEL_URL = f"{OLLAMA_HOST}/api/chat"
MODEL_NAME = "qwen3.8:27b"
TEMPERATURE = 0.4
REQUEST_TIMEOUT = 900
THINK = False
STREAM = True
MAX_OUTPUT_TOKENS = 700

# Terminal tints for CLI live stream (optional)
BOLD = "[1m"
GREEN = "[92m"
GRAY = "[90m"
RESET = "[0m"


def _c(text, colour=None):
    if colour:
        return f"{colour}{text}{RESET}"
    return f"{GRAY}{text}{RESET}"


def divider():
    print(_c("────────────────────────────────────────────────────────────", GRAY))



JSON_REMINDER = (
    "Your previous reply was not valid JSON. "
    "Reply again with ONLY a single valid JSON object. "
    "No markdown, no fences, no extra text. "
    "Use exactly the required structure with keys: "
    "response, routing, assessment, session."
)


def extract_json_object(text):
    """
    Robustly extract a JSON object from an LLM response.

    Handles:
      plain JSON
      ```json ... ```
      text surrounding JSON
    """

    text = text.strip()

    # Remove markdown fences
    text = re.sub(
        r"^```(?:json)?\s*",
        "",
        text,
        flags=re.IGNORECASE
    )

    text = re.sub(
        r"\s*```$",
        "",
        text
    )

    # First attempt: entire response
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except Exception:
        pass

    # Common model slip: trailing commas before } or ]
    try:
        fixed = re.sub(r",\s*([}\]])", r"\1", text)
        value = json.loads(fixed)
        if isinstance(value, dict):
            return value
    except Exception:
        pass

    # Find first JSON object
    start = text.find("{")

    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for i in range(
        start,
        len(text)
    ):

        char = text[i]

        if escaped:
            escaped = False
            continue

        if char == "\\":
            escaped = True
            continue

        if char == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

            if depth == 0:

                candidate = text[
                    start:i + 1
                ]

                try:
                    value = json.loads(
                        candidate
                    )

                    if isinstance(
                        value,
                        dict
                    ):
                        return value

                except Exception:
                    pass

                break

    return None


def _normalize_think(think_val):
    if isinstance(think_val, str):
        t = think_val.strip().lower()
        if t in ("none", "off", "false", "0"):
            return False
        if t in ("true", "1", "on"):
            return True
    return think_val


def _decode_json_string_prefix(buffer, start_idx):
    """Decode a JSON string starting at start_idx (first char after opening quote)."""
    i = start_idx
    out = []
    while i < len(buffer):
        ch = buffer[i]
        if ch == "\\":
            if i + 1 >= len(buffer):
                break
            nxt = buffer[i + 1]
            escapes = {
                "n": "\n", "t": "\t", "r": "\r",
                '"': '"', "\\": "\\", "/": "/",
            }
            out.append(escapes.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            break
        out.append(ch)
        i += 1
    return "".join(out)


def extract_partial_response_field(buffer):
    """
    Best-effort extract of the JSON "response" string value while the
    object is still streaming (may be incomplete / unclosed).
    Returns decoded text or None if the field has not started yet.
    """
    if not buffer:
        return None
    m = re.search(r'"response"\s*:\s*"', buffer)
    if not m:
        return None
    return _decode_json_string_prefix(buffer, m.end())


def extract_partial_routing_action(buffer):
    """
    Best-effort extract of routing.action from a partial JSON buffer.
    Returns action string or None if not yet present.
    """
    if not buffer:
        return None
    m = re.search(
        r'"routing"\s*:\s*\{[^}]*?"action"\s*:\s*"([^"]+)"',
        buffer,
        flags=re.DOTALL,
    )
    if m:
        return m.group(1).strip().lower()
    # Flat fallback if model emits action near the start
    m2 = re.search(r'"action"\s*:\s*"(continue|switch|new_subject|list_subjects|clarify)"', buffer)
    if m2:
        return m2.group(1).strip().lower()
    return None



def ask_model(messages, stream=None, on_response_delta=None, live_print=None):
    """
    Call Ollama native /api/chat.
    Returns (content: str, stats: dict).
    stats includes streamed=True when the learner-facing response was live-printed
    or pushed via on_response_delta (thread-safe; no function attributes).
    """
    think_val = _normalize_think(THINK)
    use_stream = STREAM if stream is None else bool(stream)
    if on_response_delta is not None:
        use_stream = True
    if live_print is None:
        live_print = bool(use_stream) and on_response_delta is None

    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "stream": use_stream,
        "think": think_val,
        "format": "json",
        "options": {
            "temperature": TEMPERATURE,
            "num_predict": MAX_OUTPUT_TOKENS,
        },
    }

    did_stream = False
    t0 = datetime.now()

    def _finish_stats(**extra):
        s = dict(extra)
        s.setdefault("model", MODEL_NAME)
        s.setdefault("think_requested", think_val)
        s.setdefault("streamed", did_stream)
        try:
            from metrics_lib import log_call_from_stats
            log_call_from_stats(s)
        except Exception:
            pass
        return s

    if not use_stream:
        response = requests.post(
            MODEL_URL,
            json=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        message = data.get("message") or {}
        content = message.get("content") or ""
        thinking_text = message.get("thinking") or ""
        if not content:
            content = thinking_text
        total_ns = data.get("total_duration") or 0
        prompt_ns = data.get("prompt_eval_duration") or 0
        eval_ns = data.get("eval_duration") or 0
        load_ns = data.get("load_duration") or 0
        eval_n = data.get("eval_count") or 0
        prompt_n = data.get("prompt_eval_count") or 0
        total_s = (total_ns / 1e9) if total_ns else None
        eval_s = (eval_ns / 1e9) if eval_ns else None
        stats = _finish_stats(
            thinking_returned=bool(thinking_text),
            thinking_chars=len(thinking_text),
            eval_count=eval_n,
            prompt_eval_count=prompt_n,
            total_s=round(total_s, 2) if total_s is not None else None,
            prompt_eval_s=round(prompt_ns / 1e9, 3) if prompt_ns else None,
            eval_s=round(eval_s, 3) if eval_s is not None else None,
            load_s=round(load_ns / 1e9, 3) if load_ns else None,
            ttft_s=round(prompt_ns / 1e9, 3) if prompt_ns else None,
            out_tok_per_s=round(eval_n / eval_s, 1) if eval_s and eval_n else None,
            streamed=False,
        )
        return content, stats

    # ---- streaming path ----
    response = requests.post(
        MODEL_URL,
        json=payload,
        timeout=REQUEST_TIMEOUT,
        stream=True,
    )
    response.raise_for_status()

    full_content = []
    thinking_parts = []
    eval_count = 0
    prompt_eval_count = 0
    total_ns = 0
    prompt_ns = 0
    eval_ns = 0
    load_ns = 0
    header_shown = False
    printed_len = 0
    ttft_s = None

    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue

        message = data.get("message") or {}
        piece = message.get("content") or ""
        think_piece = message.get("thinking") or ""
        if piece:
            full_content.append(piece)
            if ttft_s is None:
                ttft_s = (datetime.now() - t0).total_seconds()
        if think_piece:
            thinking_parts.append(think_piece)

        if data.get("eval_count"):
            eval_count = data["eval_count"]
        if data.get("prompt_eval_count"):
            prompt_eval_count = data["prompt_eval_count"]
        if data.get("total_duration"):
            total_ns = data["total_duration"]
        if data.get("prompt_eval_duration"):
            prompt_ns = data["prompt_eval_duration"]
        if data.get("eval_duration"):
            eval_ns = data["eval_duration"]
        if data.get("load_duration"):
            load_ns = data["load_duration"]

        buf = "".join(full_content)
        action = extract_partial_routing_action(buf)
        if action in ("continue", "clarify"):
            partial = extract_partial_response_field(buf)
            if partial is not None:
                if not header_shown:
                    if live_print:
                        print()
                        divider()
                        print(_c("TUTOR", BOLD + GREEN))
                        divider()
                        print()
                    header_shown = True
                    did_stream = True
                if len(partial) > printed_len:
                    delta = partial[printed_len:]
                    printed_len = len(partial)
                    if live_print:
                        print(delta, end="", flush=True)
                    if on_response_delta is not None:
                        try:
                            on_response_delta(partial, delta)
                        except Exception:
                            pass
                        did_stream = True
        elif action in ("list_subjects", "switch", "new_subject"):
            pass

        if data.get("done"):
            break

    if header_shown and live_print:
        print()

    content = "".join(full_content)
    thinking_text = "".join(thinking_parts)
    if not content and thinking_text:
        content = thinking_text

    elapsed = (datetime.now() - t0).total_seconds()
    total_s = (total_ns / 1e9) if total_ns else elapsed
    eval_s = (eval_ns / 1e9) if eval_ns else None
    stats = _finish_stats(
        thinking_returned=bool(thinking_text),
        thinking_chars=len(thinking_text),
        eval_count=eval_count,
        prompt_eval_count=prompt_eval_count,
        total_s=round(total_s, 2) if total_s is not None else None,
        prompt_eval_s=round(prompt_ns / 1e9, 3) if prompt_ns else None,
        eval_s=round(eval_s, 3) if eval_s is not None else None,
        load_s=round(load_ns / 1e9, 3) if load_ns else None,
        ttft_s=round(ttft_s, 3) if ttft_s is not None else (
            round(prompt_ns / 1e9, 3) if prompt_ns else None
        ),
        out_tok_per_s=round(eval_count / eval_s, 1) if eval_s and eval_count else None,
        streamed=did_stream,
    )
    return content, stats


def ask_model_with_json_retry(messages, stream=None, on_response_delta=None):
    """
    Returns (raw: str, parsed: dict|None, stats: dict).
    stats["streamed"] is True if the first call already live-printed the response.
    """
    raw, stats = ask_model(
        messages, stream=stream, on_response_delta=on_response_delta
    )
    first_streamed = bool(stats.get("streamed"))
    first_partial = ""
    try:
        first_partial = (extract_partial_response_field(raw) or "").strip()
    except Exception:
        first_partial = ""
    parsed = extract_json_object(raw)

    if parsed is not None and isinstance(parsed, dict) and (
        "response" in parsed or "routing" in parsed
    ):
        stats = dict(stats)
        stats["json_retry"] = False
        stats["streamed"] = first_streamed
        return raw, parsed, stats

    if first_streamed:
        print()
        print(_c("(antwoord opnieuw opbouwen…)", GRAY))
        print()

    failed_preview = (raw or "")[:400]
    retry_messages = list(messages) + [
        {"role": "assistant", "content": failed_preview},
        {"role": "user", "content": JSON_REMINDER},
    ]

    try:
        raw2, stats2 = ask_model(retry_messages, stream=False)
        stats2 = dict(stats2)
        stats2["json_retry"] = True
        parsed2 = extract_json_object(raw2)
        final_resp = ""
        if isinstance(parsed2, dict):
            final_resp = str(parsed2.get("response") or "").strip()
        if first_streamed and final_resp and first_partial:
            if final_resp == first_partial or final_resp[:100] == first_partial[:100]:
                stats2["streamed"] = True
            else:
                stats2["streamed"] = False
        else:
            stats2["streamed"] = False
        if parsed2 is not None:
            return raw2, parsed2, stats2
        return raw2, None, stats2
    except Exception:
        stats = dict(stats)
        stats["streamed"] = first_streamed
        stats["json_retry"] = True
        return raw, None, stats


def print_model_stats(stats=None):
    """Show whether CoT ran and how long the call took."""
    s = stats or {}
    if not s:
        return
    think_req = s.get("think_requested")
    think_got = s.get("thinking_returned")
    chars = s.get("thinking_chars") or 0
    eval_n = s.get("eval_count") or 0
    prompt_n = s.get("prompt_eval_count") or 0
    secs = s.get("total_s")
    streamed = s.get("streamed")
    parts = [
        f"think={think_req}",
        f"cot={'yes' if think_got else 'no'}"
        + (f"({chars}c)" if chars else ""),
        f"out={eval_n}tok",
        f"in={prompt_n}tok",
    ]
    if s.get("ttft_s") is not None:
        parts.append(f"ttft={s['ttft_s']}s")
    if s.get("out_tok_per_s") is not None:
        parts.append(f"{s['out_tok_per_s']}tok/s")
    if streamed:
        parts.append("stream")
    if getattr(ask_model, "last_json_retry", False):
        parts.append("json_retry")
    if secs is not None:
        parts.append(f"{secs}s")
    print(_c("  [" + " | ".join(parts) + "]", GRAY))
    # Optional one-line component sizes from last build
    try:
        from metrics_lib import get_last_breakdown
        bd = get_last_breakdown()
        if bd and bd.get("components"):
            top = sorted(
                bd["components"],
                key=lambda x: -(x.get("tokens_est") or 0),
            )[:3]
            bits = [f"{x['name']}≈{x.get('tokens_est', 0)}" for x in top]
            est = bd.get("total_tokens_est")
            ph = bd.get("prefix_hash") or "-"
            print(_c(
                f"  [ctx est≈{est} | prefix={ph} | top: {', '.join(bits)}]",
                GRAY,
            ))
    except Exception:
        pass


JSON_REMINDER = (
    "Your previous reply was not valid JSON. "
    "Reply again with ONLY a single valid JSON object. "
    "No markdown, no fences, no extra text. "
    "Use exactly the required structure with keys: "
    "response, routing, assessment, session."
)


