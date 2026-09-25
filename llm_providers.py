# -*- coding: utf-8 -*-
"""
Provider-agnostic structured LLM calls for the AI cleaning passes + the cell-line matcher.

Four providers behind one async `classify()`:
  - anthropic : AsyncAnthropic, forced tool-use (+ prompt caching)
  - openai    : AsyncOpenAI chat.completions, forced function-call
  - gemini    : the SAME AsyncOpenAI SDK pointed at Google's OpenAI-compatible endpoint
  - ollama    : the SAME AsyncOpenAI SDK pointed at a LOCAL Ollama server (no API key; default
                http://localhost:11434/v1, override via the OLLAMA_HOST env var). Use a tool-capable
                model (llama3.1 / llama3.2 / qwen2.5 / mistral-nemo / ...) for the structured passes.

Each call takes a `tool` = {name, description, input_schema} whose input_schema is an object with a
`results` array, and returns (results_list, usage_dict). The OpenAI/Gemini path defensively falls
back to parsing JSON from the message content if a model returns the object inline instead of as a
tool call.

API keys (env): ANTHROPIC_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY (or GOOGLE_API_KEY). Ollama needs
none (set OLLAMA_API_KEY only if you put Ollama behind an authenticated proxy).
"""
import json
import os
import re

try:                                  # httpx ships with the openai SDK; used by the Ollama native path
    import httpx
except Exception:                     # pragma: no cover - degrade with a clear runtime error if absent
    httpx = None

GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai/"
# Ollama serves an OpenAI-compatible API locally and needs NO key. It IGNORES the shared base_url (that
# field is the OpenAI custom-endpoint); point Ollama elsewhere with the standard OLLAMA_HOST env var.
OLLAMA_OPENAI_BASE = "http://localhost:11434/v1"

# OpenAI-compatible switch to turn OFF a reasoning model's chain-of-thought (e.g. gpt-oss or Xiaomi MiMo,
# whose "thinking" can eat most of the max_tokens budget -> slow at a fixed tok/s, and can truncate the
# tool call -> empty results). Sent via extra_body ONLY when the caller opts into "disable reasoning".
# Hosts obey DIFFERENT knobs, so we send all three and let each host ignore the rest:
#   reasoning_effort -> OpenAI o-series + gpt-oss (these IGNORE thinking/enable_thinking, so without this
#                       line "disable reasoning" was a silent no-op for gpt-oss);
#   thinking         -> Anthropic-style hosts;
#   enable_thinking  -> MiMo / self-hosted vLLM (--reasoning-parser) chat-template hosts.
# Unknown fields are dropped by LiteLLM/most proxies; a host that hard-400s is retried WITHOUT the body and
# remembered (see _create_with_reasoning_fallback below).
NO_REASONING_BODY = {"reasoning_effort": "low",
                     "thinking": {"type": "disabled"},
                     "chat_template_kwargs": {"enable_thinking": False}}

# (base_url, model) endpoints that REJECTED the body above (HTTP 400/422 -- e.g. api.openai.com or Gemini's
# OpenAI-compat endpoint refusing the unknown `thinking` / `chat_template_kwargs` fields, or a non-reasoning
# model refusing `reasoning_effort`). The compounds pass ALWAYS asks for reasoning off (it is knowledge
# retrieval), so without this fallback every compounds batch against such an endpoint failed -> retries ->
# batches dropped -> the whole pass Unknown-filled, while the preflight (which doesn't send the body) passed.
# Now: retry that request once WITHOUT the body and never send it to that endpoint+model again.
_NO_REASONING_REJECTED = set()


def _endpoint_key(client, model):
    return (str(getattr(client, "base_url", "") or ""), model or "")


def _is_bad_request(e):
    return getattr(e, "status_code", None) in (400, 422)


async def _create_with_reasoning_fallback(client, model, kwargs, disable_reasoning):
    """chat.completions.create with the no-reasoning body when requested, transparently retried without it
    if this endpoint+model rejects the body (400/422). Used by classify() and chat()."""
    key = _endpoint_key(client, model)
    if disable_reasoning and key not in _NO_REASONING_REJECTED:
        kwargs["extra_body"] = NO_REASONING_BODY
    try:
        return await client.chat.completions.create(**kwargs)
    except Exception as e:
        if "extra_body" in kwargs and _is_bad_request(e):
            _NO_REASONING_REJECTED.add(key)
            kwargs.pop("extra_body", None)
            return await client.chat.completions.create(**kwargs)
        raise

PROVIDERS = ("anthropic", "openai", "gemini", "ollama")
PROVIDER_LABEL = {"anthropic": "Anthropic (Claude)", "openai": "OpenAI (ChatGPT)",
                  "gemini": "Google Gemini", "ollama": "Ollama (local)"}
KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "gemini": "GEMINI_API_KEY",
           "ollama": "OLLAMA_API_KEY"}
DEFAULT_MODEL = {"anthropic": "claude-haiku-4-5", "openai": "gpt-5.4-nano",
                 "gemini": "gemma-4-31b-it", "ollama": "llama3.1"}
# suggested models per provider (the UI model box is EDITABLE — these are just autocomplete hints;
# type any exact model id your account has access to). First entry is the per-provider default.
MODELS = {
    "anthropic": ["claude-haiku-4-5", "claude-sonnet-4-6"],
    "openai": ["gpt-5.4-nano", "gpt-4.1-mini", "gpt-4.1", "gpt-4o", "gpt-4o-mini"],
    "gemini": ["gemma-4-31b-it", "gemini-2.5-flash", "gemini-3.5-flash", "gemini-2.5-pro",
               "gemma-3-27b-it"],
    # Ollama models must be pulled locally first (`ollama pull <model>`); use tool-capable ones.
    "ollama": ["llama3.1", "llama3.2", "qwen2.5", "mistral-nemo", "firefunction-v2", "command-r"],
}


def normalize_provider(p):
    p = (p or "anthropic").strip().lower()
    if p in ("chatgpt", "gpt"):
        p = "openai"
    if p in ("google", "google-gemini"):
        p = "gemini"
    return p if p in PROVIDERS else "anthropic"


def resolve_model(provider, model):
    return (model or "").strip() or DEFAULT_MODEL.get(provider, DEFAULT_MODEL["anthropic"])


def api_key(provider):
    # Always .strip(): a key pasted with a stray leading/trailing space or TAB otherwise reaches the gateway
    # as `Bearer \tsk-…`, which fails auth (e.g. LiteLLM master-key mismatch -> the misleading "No connected db").
    if provider == "gemini":
        return (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if provider == "ollama":
        # Ollama is local and needs no key, but the OpenAI SDK requires a non-empty value -> placeholder.
        # Set OLLAMA_API_KEY only for an authenticated Ollama proxy.
        return (os.environ.get("OLLAMA_API_KEY", "") or "ollama").strip()
    return (os.environ.get(KEY_ENV.get(provider, ""), "") or "").strip()


def have_key(provider):
    return bool(api_key(provider))


def make_client(provider, max_retries=8, timeout=None, base_url=None):
    """Create the async client for a provider (anthropic, openai, or gemini-via-openai-compat).

    `timeout` (seconds), when set, caps each request — used by the AI preflight so a bad/slow
    endpoint fails fast instead of blocking on the SDK's long default.
    `base_url`, when set, points the provider at a CUSTOM OpenAI-compatible endpoint — e.g. running
    an OpenAI-format model (MiMo, Qwen, a local vLLM/LM-Studio server, OpenRouter, …) through the
    'openai' provider. Blank/None => the provider's default endpoint (api.openai.com for 'openai';
    the Gemini OpenAI-compat URL for 'gemini')."""
    extra = {} if timeout is None else {"timeout": timeout}
    base_url = (base_url or "").strip() or None
    if provider == "anthropic":
        from anthropic import AsyncAnthropic
        if base_url:
            extra["base_url"] = base_url
        return AsyncAnthropic(api_key=api_key("anthropic") or None, max_retries=max_retries, **extra)
    from openai import AsyncOpenAI
    if provider == "gemini":
        return AsyncOpenAI(api_key=api_key("gemini"), base_url=base_url or GEMINI_OPENAI_BASE,
                           max_retries=max_retries, **extra)
    if provider == "ollama":
        # Ollama is LOCAL by design: IGNORE the passed base_url (that's the OpenAI custom-endpoint, e.g.
        # MiMo) so selecting Ollama can never accidentally hit a cloud host with the placeholder key
        # (which 401s as "API key rejected"). Default to localhost; honor the standard OLLAMA_HOST env
        # var for a custom local port / LAN box.
        oh = (os.environ.get("OLLAMA_HOST") or "").strip()
        if oh:
            if not oh.startswith(("http://", "https://")):
                oh = "http://" + oh
            oh = oh.rstrip("/")
            ob = oh if oh.endswith("/v1") else oh + "/v1"
        else:
            ob = OLLAMA_OPENAI_BASE
        return AsyncOpenAI(api_key=api_key("ollama"), base_url=ob, max_retries=max_retries, **extra)
    if base_url:                                  # custom OpenAI-compatible host (MiMo etc.)
        extra["base_url"] = base_url
    return AsyncOpenAI(api_key=api_key("openai") or None, max_retries=max_retries, **extra)  # stripped key (env)


async def close_client(client):
    try:
        await client.close()
    except Exception:
        pass


def _extract_results(text):
    """Defensive: pull a `results` array out of free-text/JSON (handles ```json fences)."""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    try:
        obj = json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)
        obj = json.loads(m.group(0)) if m else {}
    if isinstance(obj, dict):
        return obj.get("results", [])
    return obj if isinstance(obj, list) else []


def _ollama_native_url():
    """Native /api/chat URL. The OpenAI-compat /v1 path can't grammar-constrain output (`format`) nor
    disable a thinking model's chain-of-thought, so the ollama path uses the native API. Host
    resolution mirrors make_client: OLLAMA_HOST env (with/without scheme or /v1) else localhost."""
    oh = (os.environ.get("OLLAMA_HOST") or "").strip()
    if oh:
        if not oh.startswith(("http://", "https://")):
            oh = "http://" + oh
        oh = oh.rstrip("/")
        if oh.endswith("/v1"):
            oh = oh[:-3].rstrip("/")
        return oh + "/api/chat"
    return "http://localhost:11434/api/chat"


async def _classify_ollama_native(model, system_text, user_content, tool, max_tokens,
                                  disable_reasoning):
    """Ollama via the NATIVE /api/chat with grammar-constrained structured output (`format`=schema)
    plus `think:false`. `format` forces the reply to BE the {results:[...]} object, so the model can't
    spend the budget on a chain-of-thought before a tool call — the OpenAI-compat tool path let gemma
    emit ~200 think-tokens/item; this is ~40 (the actual JSON). No tool-calling (more reliable for
    small local models). Raises on HTTP error so the caller's retry/diagnose path handles it."""
    if httpx is None:
        raise RuntimeError("httpx is not installed but is required for the Ollama provider (it ships "
                           "with the openai package). Run `pip install httpx`, or pick a cloud provider.")
    sys_txt = system_text + ('\nReturn ONLY a JSON object {"results":[...]} containing exactly one '
                             "element per input string, preserving each exact raw value. Do not "
                             "call any tool; output only the JSON object.")
    body = {
        "model": model, "stream": False,
        "format": tool["input_schema"],                 # JSON-schema grammar -> output IS the object
        "options": {"num_predict": max_tokens, "temperature": 0},
        "messages": [{"role": "system", "content": sys_txt},
                     {"role": "user", "content": user_content}],
    }
    if disable_reasoning:
        body["think"] = False
    url = _ollama_native_url()
    timeout = httpx.Timeout(900.0, connect=10.0)         # CPU-local models are slow; don't cut a batch off
    async with httpx.AsyncClient(timeout=timeout) as hc:
        try:
            r = await hc.post(url, json=body)
            r.raise_for_status()
        except httpx.HTTPStatusError as e:
            body_txt = ""
            try:
                body_txt = (e.response.text or "").lower()
            except Exception:
                pass
            if "think" in body and "think" in body_txt:  # model doesn't accept think -> retry without
                body.pop("think", None)
                r = await hc.post(url, json=body)
                r.raise_for_status()
            else:
                raise
        try:
            resp = r.json()
        except Exception as je:        # invalid/empty body -> treat as a (retryable) network error
            raise RuntimeError("Ollama network error: invalid/empty JSON from /api/chat (%s)" % je)
    content = (resp.get("message") or {}).get("content", "") or ""
    results = _extract_results(content)
    usage = {"input_tokens": resp.get("prompt_eval_count", 0) or 0,
             "output_tokens": resp.get("eval_count", 0) or 0,
             "cache_read": 0, "cache_creation": 0}
    return results, usage


async def classify(client, provider, model, system_text, user_obj, tool, max_tokens=8000,
                   disable_reasoning=False):
    """Run one structured batch. Returns (results_list, usage_dict).

    disable_reasoning: for the openai/gemini path, send the OpenAI-compatible "turn thinking off"
    body (NO_REASONING_BODY) so a reasoning model (MiMo etc.) doesn't burn the token budget on
    chain-of-thought and truncate the tool call. Ignored for anthropic (claude-haiku doesn't think)."""
    name = tool["name"]
    user_content = json.dumps(user_obj, ensure_ascii=False)

    if provider == "anthropic":
        system = [{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}]
        async with client.messages.stream(
            model=model, max_tokens=max_tokens, system=system,
            tools=[dict(tool, **{"strict": True})],
            tool_choice={"type": "tool", "name": name},
            messages=[{"role": "user", "content": user_content}],
        ) as stream:
            msg = await stream.get_final_message()
        tool_input = next((b.input for b in msg.content if b.type == "tool_use"), None)
        results = (tool_input or {}).get("results", [])
        u = msg.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read": getattr(u, "cache_read_input_tokens", 0),
                 "cache_creation": getattr(u, "cache_creation_input_tokens", 0)}
        return results, usage

    if provider == "ollama":            # local: native structured-output path (see _classify_ollama_native)
        return await _classify_ollama_native(
            model, system_text, user_content, tool, max_tokens, disable_reasoning)

    # ---- openai / gemini (OpenAI-compatible) ----
    func = {"type": "function", "function": {
        "name": name, "description": tool.get("description", ""),
        "parameters": tool["input_schema"]}}
    kwargs = dict(
        model=model, max_tokens=max_tokens,
        messages=[{"role": "system", "content": system_text},
                  {"role": "user", "content": user_content}],
        tools=[func],
        # openai: force the exact function. gemini: "required". (ollama uses the native path above.)
        tool_choice=({"type": "function", "function": {"name": name}} if provider == "openai"
                     else "required"),
    )
    resp = await _create_with_reasoning_fallback(client, model, kwargs, disable_reasoning)
    choice = resp.choices[0].message
    results = []
    tcs = getattr(choice, "tool_calls", None)
    if tcs:
        try:
            results = json.loads(tcs[0].function.arguments).get("results", [])
        except Exception:
            results = []
    if not results and getattr(choice, "content", None):
        try:
            results = _extract_results(choice.content)
        except Exception:
            results = []
    u = getattr(resp, "usage", None)
    usage = {"input_tokens": getattr(u, "prompt_tokens", 0) if u else 0,
             "output_tokens": getattr(u, "completion_tokens", 0) if u else 0,
             "cache_read": 0, "cache_creation": 0}
    return results, usage


# ===================== general agentic chat-with-tools (the Assistant panel) =====================
# Unlike classify() (one forced structured tool call), chat() is a multi-turn loop where the MODEL
# decides which tools to call. The caller (chat_assist) keeps a PROVIDER-NEUTRAL history and we convert
# it to each provider's native shape here, so the orchestrator never sees provider details.
#
# Neutral history entries (list of dicts):
#   {"role":"user","content": str}
#   {"role":"assistant","content": str, "tool_calls":[{"id","name","input":dict}]}   (tool_calls optional)
#   {"role":"tool","tool_call_id": str, "content": str}
# Tools: list of {name, description, input_schema}  (Anthropic-native shape; converted for OpenAI).

def _to_anthropic_messages(messages):
    """Neutral history -> Anthropic messages (consecutive tool results coalesce into ONE user msg)."""
    out, i, n = [], 0, len(messages)
    while i < n:
        m = messages[i]
        role = m.get("role")
        if role == "assistant":
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"],
                               "input": tc.get("input") or {}})
            out.append({"role": "assistant", "content": blocks or m.get("content", "")})
            i += 1
        elif role == "tool":
            results = []
            while i < n and messages[i].get("role") == "tool":      # Anthropic wants all tool_results together
                t = messages[i]
                results.append({"type": "tool_result", "tool_use_id": t.get("tool_call_id"),
                                "content": t.get("content", "")})
                i += 1
            out.append({"role": "user", "content": results})
        else:                                                       # user (or unknown -> treat as user text)
            out.append({"role": "user", "content": m.get("content", "")})
            i += 1
    return out


def _to_openai_messages(messages, system):
    out = [{"role": "system", "content": system}] if system else []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            a = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                a["tool_calls"] = [{"id": tc["id"], "type": "function",
                                    "function": {"name": tc["name"],
                                                 "arguments": json.dumps(tc.get("input") or {})}}
                                   for tc in m["tool_calls"]]
            out.append(a)
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id"),
                        "content": m.get("content", "")})
        else:
            out.append({"role": "user", "content": m.get("content", "")})
    return out


async def chat(client, provider, model, messages, tools=None, system="", max_tokens=4096,
               disable_reasoning=False):
    """One model round with optional tool-calling. Returns a normalized turn:
       {"text": str, "tool_calls": [{"id","name","input"(dict)}], "stop_reason": str, "usage": {...}}.
    The caller appends the assistant turn + each tool result to `messages` and calls again until
    `tool_calls` is empty (the model produced its final answer)."""
    if provider == "anthropic":
        kwargs = dict(model=model, max_tokens=max_tokens, messages=_to_anthropic_messages(messages))
        if system:
            kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        if tools:
            kwargs["tools"] = list(tools)
            kwargs["tool_choice"] = {"type": "auto"}
        msg = await client.messages.create(**kwargs)
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        tcs = [{"id": b.id, "name": b.name, "input": b.input}
               for b in msg.content if getattr(b, "type", "") == "tool_use"]
        u = msg.usage
        usage = {"input_tokens": getattr(u, "input_tokens", 0), "output_tokens": getattr(u, "output_tokens", 0),
                 "cache_read": getattr(u, "cache_read_input_tokens", 0),
                 "cache_creation": getattr(u, "cache_creation_input_tokens", 0)}
        return {"text": text, "tool_calls": tcs, "stop_reason": getattr(msg, "stop_reason", ""),
                "usage": usage}

    # ---- openai / gemini / ollama (OpenAI-compatible /v1 tool-calling) ----
    kwargs = dict(model=model, max_tokens=max_tokens, messages=_to_openai_messages(messages, system))
    if tools:
        kwargs["tools"] = [{"type": "function",
                            "function": {"name": t["name"], "description": t.get("description", ""),
                                         "parameters": t["input_schema"]}} for t in tools]
        kwargs["tool_choice"] = "auto"
    resp = await _create_with_reasoning_fallback(client, model, kwargs, disable_reasoning)
    choice = resp.choices[0].message
    text = getattr(choice, "content", None) or ""
    tcs = []
    for tc in (getattr(choice, "tool_calls", None) or []):
        try:
            args = json.loads(tc.function.arguments or "{}")
        except Exception:
            args = {}
        tcs.append({"id": tc.id, "name": tc.function.name, "input": args})
    u = getattr(resp, "usage", None)
    usage = {"input_tokens": getattr(u, "prompt_tokens", 0) if u else 0,
             "output_tokens": getattr(u, "completion_tokens", 0) if u else 0,
             "cache_read": 0, "cache_creation": 0}
    return {"text": text, "tool_calls": tcs,
            "stop_reason": getattr(resp.choices[0], "finish_reason", ""), "usage": usage}


def classify_ai_error(exc):
    """Turn an AI-provider exception into an actionable diagnosis {category, title, detail}.
    Categories: auth | model | perm | rate | network | unknown. Used to tell the user whether to fix
    the API key, the model name, the provider, etc. — or turn AI off."""
    msg = str(exc or "")
    low = msg.lower()
    status = (getattr(exc, "status_code", None)
              or getattr(getattr(exc, "response", None), "status_code", None))

    def D(category, title, detail):
        return {"category": category, "title": title, "detail": detail}

    # httpx transport errors (the local-Ollama native path) carry no HTTP status code, and their str()
    # is often empty -> classify by TYPE as network so a transient/local-down blip retries (not "auth").
    if httpx is not None and isinstance(exc, httpx.HTTPError) and not isinstance(exc, httpx.HTTPStatusError):
        return D("network", "Couldn't reach the provider",
                 "Network/transport error contacting the AI provider (e.g. local Ollama is down, "
                 "updating, or unreachable). Check it's running, switch provider, or turn off AI cleaning.")

    if status == 401 or any(s in low for s in ("api key", "api_key", "unauthorized", "authenticat",
                                               "invalid x-api-key", "no auth credentials",
                                               "invalid_api_key")):
        return D("auth", "API key rejected",
                 "The API key was missing or invalid for this provider. Paste a valid key, switch "
                 "provider, or turn off AI cleaning.")
    if status == 404 or any(s in low for s in ("not found", "does not exist", "is not a valid model",
                                               "unknown model", "no such model", "model_not_found")):
        return D("model", "Model not found",
                 "That model name isn't valid for this provider/account. Fix the model id, switch "
                 "provider, or turn off AI cleaning.")
    if status == 403 or "permission" in low or ("access" in low and "denied" in low):
        return D("perm", "Access denied for this model",
                 "Your account can't access this model. Use a different model/provider, or turn off "
                 "AI cleaning.")
    # OpenAI-compatible PROXY (LiteLLM / vLLM / a local gateway) that is reachable but has no model or
    # database backend connected right now — typically returned as a 400 with one of these phrases. This
    # is NOT a bad request from us: the proxy needs its model/DB reconnected (or restarting).
    if any(s in low for s in ("no connected db", "no_db_connection", "no healthy deployment",
                              "no deployments available", "no model deployed", "no available deployment")):
        return D("network", "AI proxy/endpoint not ready",
                 "The OpenAI-compatible endpoint at your base URL (e.g. a local LiteLLM/vLLM proxy) is "
                 "reachable but has no model/database backend connected right now. Restart or reconnect "
                 "the proxy (and its model), switch provider, or turn off AI cleaning.")
    if status == 429 or any(s in low for s in ("rate limit", "ratelimit", "quota",
                                               "resource_exhausted", "too many requests")):
        return D("rate", "Rate limit / quota exceeded",
                 "The provider is rate-limiting this key (often a free tier). Lower concurrency, wait, "
                 "switch provider, or turn off AI cleaning.")
    if any(s in low for s in ("timed out", "timeout", "connection", "network", "temporary failure",
                              "getaddrinfo", "ssl")):
        return D("network", "Couldn't reach the provider",
                 "Network error contacting the AI provider. Check connectivity / base URL, switch "
                 "provider, or turn off AI cleaning.")
    return D("unknown", "AI provider call failed",
             (msg[:200] or "The AI provider call failed.")
             + "  Fix the provider / model / key, or turn off AI cleaning.")
