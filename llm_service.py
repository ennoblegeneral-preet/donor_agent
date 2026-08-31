import os
import json
import threading
import requests
from dotenv import load_dotenv
from error_utils import classify_error

load_dotenv()

# Lightweight per-thread token counter, used to get a one-off estimate of how many
# tokens a full pipeline run (research + financials/CSR + scoring) costs for a
# single company - not a permanent per-company ledger, just enough to size up
# what switching LLM providers/models would cost. threading.local() (not a plain
# module global) because each company's pipeline runs in its own background
# thread (see app.py's run_company_pipeline) - this keeps concurrent runs' counts
# from bleeding into each other.
_tracker = threading.local()


def start_tracking():
    """Call at the start of a pipeline run to begin accumulating token usage."""
    _tracker.usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}


def get_tracked_usage():
    """Returns the accumulated usage dict for the current thread, or None if
    start_tracking() was never called on it."""
    return getattr(_tracker, "usage", None)


def _record_usage(usage: dict):
    tracker = getattr(_tracker, "usage", None)
    if tracker is None or not usage:
        return
    tracker["prompt_tokens"] += usage.get("prompt_tokens") or 0
    tracker["completion_tokens"] += usage.get("completion_tokens") or 0
    tracker["total_tokens"] += usage.get("total_tokens") or 0
    tracker["calls"] += 1

def get_llm_config() -> dict:
    """
    Reads environment configuration to determine active LLM provider and settings.
    Supported providers: 'ollama' | 'groq' | 'openai'
    """
    provider = os.getenv("LLM_PROVIDER", "").lower().strip()
    if not provider:
        if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
            provider = "gemini"
        elif os.getenv("NVIDIA_API_KEY") or os.getenv("NIVIDA_API_KEY") or os.getenv("nvidia_api_key") or os.getenv("nividia_api_key"):
            provider = "nvidia"
        elif os.getenv("USE_OLLAMA", "").lower() == "true":
            provider = "ollama"
        elif os.getenv("GROQ_API_KEY") or os.getenv("qroq_api"):
            provider = "groq"
        elif os.getenv("OPENAI_API_KEY"):
            provider = "openai"
        else:
            provider = "ollama"  # Default to local Ollama if unconfigured

    if provider == "ollama":
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/")
        # Native /api/chat, not the OpenAI-compat /v1/chat/completions - verified
        # empirically that the OpenAI-compat endpoint silently IGNORES num_ctx
        # overrides and always loads the model with Ollama's 4096-token default,
        # which truncates this app's larger prompts (research extraction alone
        # sends up to ~38k chars / ~9-10k tokens). The native endpoint actually
        # honors options.num_ctx.
        if base_url.endswith("/v1"):
            base_url = base_url[: -len("/v1")].rstrip("/")
        model = os.getenv("OLLAMA_MODEL", "llama3.2")
        headers = {"Content-Type": "application/json"}
        endpoint = f"{base_url}/api/chat"
    elif provider == "groq":
        api_key = (os.getenv("GROQ_API_KEY") or os.getenv("qroq_api") or "").strip().strip('"').strip("'")
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        endpoint = "https://api.groq.com/openai/v1/chat/completions"
    elif provider == "openai":
        api_key = (os.getenv("OPENAI_API_KEY") or "").strip().strip('"').strip("'")
        model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        endpoint = "https://api.openai.com/v1/chat/completions"
    elif provider in ("gemini", "google"):
        api_key = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip().strip('"').strip("'")
        model = os.getenv("GEMINI_MODEL") or os.getenv("GOOGLE_MODEL") or "gemini-3.6-flash"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        endpoint = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    elif provider in ("nvidia", "nividia"):
        api_key = (os.getenv("NVIDIA_API_KEY") or os.getenv("NIVIDA_API_KEY") or os.getenv("nvidia_api_key") or os.getenv("nividia_api_key") or "").strip().strip('"').strip("'")
        model = os.getenv("NVIDIA_MODEL") or os.getenv("NIVIDA_MODEL") or "nvidia/nemotron-3-ultra-550b-a55b"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        endpoint = "https://integrate.api.nvidia.com/v1/chat/completions"
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")

    return {
        "provider": provider,
        "endpoint": endpoint,
        "model": model,
        "headers": headers
    }

def call_llm(prompt: str, json_mode: bool = True, temperature: float = 0.0, timeout: int = 600):
    """
    Sends a prompt to the active LLM provider (Ollama / Groq / OpenAI).
    If json_mode is True, returns a parsed Python dictionary.

    LLM_TIMEOUT in .env overrides whatever timeout each call site passes -
    one knob to raise for slow local (CPU-only Ollama) inference, instead of
    editing timeout= at every call site individually.
    """
    cfg = get_llm_config()
    timeout = int(os.getenv("LLM_TIMEOUT", timeout))

    payload = {
        "model": cfg["model"],
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }

    if cfg["provider"] == "ollama":
        # Native /api/chat request/response shape differs from the OpenAI format -
        # "stream" must be explicit (else it streams NDJSON chunks instead of one
        # JSON object), "format": "json" is Ollama's JSON-mode equivalent, and
        # options.num_ctx is what actually stops prompts being silently truncated
        # to Ollama's 4096-token default. OLLAMA_NUM_CTX in .env can raise this
        # further if prompts grow beyond what the default covers.
        payload["stream"] = False
        payload["options"] = {"num_ctx": int(os.getenv("OLLAMA_NUM_CTX", 16384))}
        if json_mode:
            payload["format"] = "json"
    elif json_mode:
        payload["response_format"] = {"type": "json_object"}

    response = requests.post(
        cfg["endpoint"],
        headers=cfg["headers"],
        json=payload,
        timeout=timeout
    )
    response.raise_for_status()

    res_json = response.json()
    if cfg["provider"] == "ollama":
        content = res_json["message"]["content"].strip()
        prompt_tokens = res_json.get("prompt_eval_count") or 0
        completion_tokens = res_json.get("eval_count") or 0
        _record_usage({
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        })
    else:
        content = res_json["choices"][0]["message"]["content"].strip()
        _record_usage(res_json.get("usage") or {})

    if json_mode:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Fallback substring extraction if output is wrapped in ```json ... ```
            start = content.find("{")
            end = content.rfind("}") + 1
            if start != -1 and end > start:
                return json.loads(content[start:end])
            raise
    
    return content

def call_llm_safe(prompt: str, json_mode: bool = True, temperature: float = 0.0, timeout: int = 60) -> tuple:
    """
    Safe wrapper around call_llm returning (result_dict, error_dict).
    """
    try:
        data = call_llm(prompt=prompt, json_mode=json_mode, temperature=temperature, timeout=timeout)
        return data, None
    except Exception as e:
        # classify_error() deliberately sanitizes what reaches the DB/frontend (never
        # leak raw exception text - could contain internal URLs/keys) - print the raw
        # exception here so the real cause is still visible in server logs.
        print(f"[LLM Error] call_llm failed: {e}")
        err = classify_error(e, context="llm")
        return {}, err
