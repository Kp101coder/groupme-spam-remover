from typing import Any, Dict, List, Optional
import ollama


# Update these module-level values to customize the model or Ollama host.
MODEL = "deepseek-r1:14b"
OLLAMA_HOST = "http://192.168.4.212:11434"  # Required; service will fail fast if unreachable

ollama_model = ollama.Client(host=OLLAMA_HOST)

def get_model() -> str:
    """Return the name of the active model."""
    return MODEL

def get_host() -> Optional[str]:
    """Return the configured Ollama host URL or None if using the default."""
    return OLLAMA_HOST

def _model_exists(name: str) -> bool:
    response = ollama_model.list() or {}
    models = response.get("models", []) if isinstance(response, dict) else response
    available_models = [m.get("model") for m in models if isinstance(m, dict)]
    return name in available_models


def check_model_availability() -> bool:
    """Return True if the active model is available (may raise on client errors)."""
    return _model_exists(MODEL)

def pull_model() -> None:
    """Pull the configured model. Caller should handle exceptions."""
    ollama_model.pull(MODEL)

def set_model(model_name: str) -> str:
    """Switch to a different model if it has been downloaded. Returns the active model."""
    if not isinstance(model_name, str) or not model_name.strip():
        raise ValueError("Model name must be a non-empty string")
    candidate = model_name.strip()
    if not _model_exists(candidate):
        raise ValueError(f"Model '{candidate}' is not available on the Ollama host")
    global MODEL
    MODEL = candidate
    return MODEL

def parse_yes_no_label(text: str) -> Optional[str]:
    """Return 'Yes' or 'No' if the model output starts or ends with either, else None."""
    if not text:
        return None
    parts = text.strip().lower().split()
    if not parts:
        return None
    first = parts[0]
    last = parts[-1]
    if last == "yes":
        return "Yes"
    if last == "no":
        return "No"
    if first == "yes":
        return "Yes"
    if first == "no":
        return "No"
    return None


def list_models() -> dict:
    """Return ollama list() output (dict)."""
    return ollama_model.list()


def pull_model_name(name: str) -> None:
    """Pull an arbitrary model by name."""
    ollama_model.pull(name)


def remove_model(name: str) -> None:
    """Remove an arbitrary model by name."""
    ollama_model.delete(name)


def _ns_to_seconds(value: Any) -> Optional[float]:
    if not isinstance(value, (int, float)):
        return None
    return round(float(value) / 1_000_000_000, 6)


def _coerce_to_dict(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict):
        return obj
    if obj is None:
        return {}
    model_dump = getattr(obj, "model_dump", None)
    if callable(model_dump):
        try:
            dumped = model_dump()
            if isinstance(dumped, dict):
                return dumped
        except Exception:
            pass
    try:
        return dict(obj)
    except Exception:
        pass
    try:
        return vars(obj)
    except Exception:
        return {}


def _extract_content(raw_content: str) -> Dict[str, Optional[str]]:
    if not raw_content:
        return {"content": None, "thinking": None, "raw_content": None}

    thinking_text: Optional[str] = None
    final_content = raw_content

    if "</think>" in raw_content:
        think_start = raw_content.find("<think>")
        think_end = raw_content.find("</think>")
        if think_start != -1 and think_end != -1 and think_end > think_start:
            start_idx = think_start + len("<think>")
            thinking_text = raw_content[start_idx:think_end].strip()
            final_content = raw_content[think_end + len("</think>"):].strip()
        else:
            final_content = raw_content.strip()
    else:
        final_content = raw_content.strip()

    return {
        "content": final_content or None,
        "thinking": thinking_text,
        "raw_content": raw_content,
    }


def prompt(message: str, system_message: str = None, data: List[dict] = None, train_start: Optional[str] = None, train_end: Optional[str] = None, think: bool = False) -> Optional[Dict[str, Any]]:
    """Send a prompt to the model and return a structured response.

    Returns a dict containing fields such as model, content, thinking, token counts,
    and timing information (converted to seconds). Callers should handle errors.
    """
    messages = []

    if system_message:
        messages.append({"role": "system", "content": system_message})

    if data:
        if train_start:
            messages.append({"role": "user", "content": train_start})

        for entry in data:
            messages.append(entry)

        if train_end:
            messages.append({"role": "user", "content": train_end})

    # Add current message
    messages.append({"role": "user", "content": message})

    response = ollama_model.chat(
        model=MODEL,
        messages=messages,
        stream=False,
        think=think
    )
    response_dict = response if isinstance(response, dict) else _coerce_to_dict(response)
    if not response_dict:
        return {"model": MODEL, "content": str(response)}

    message_block = _coerce_to_dict(response_dict.get("message"))
    raw_content = message_block.get("content") or ""
    extracted = _extract_content(raw_content)

    thinking_text = extracted["thinking"]
    if not thinking_text:
        raw_think = message_block.get("thinking") or response_dict.get("thinking")
        if isinstance(raw_think, str):
            thinking_text = raw_think.strip() or None

    top_level: Dict[str, Any] = {
        "model": response_dict.get("model", MODEL),
        "created_at": response_dict.get("created_at"),
        "done": response_dict.get("done"),
        "done_reason": response_dict.get("done_reason"),
        "prompt_eval_count": response_dict.get("prompt_eval_count"),
        "eval_count": response_dict.get("eval_count"),
        "total_duration_s": _ns_to_seconds(response_dict.get("total_duration")),
        "load_duration_s": _ns_to_seconds(response_dict.get("load_duration")),
        "prompt_eval_duration_s": _ns_to_seconds(response_dict.get("prompt_eval_duration")),
        "eval_duration_s": _ns_to_seconds(response_dict.get("eval_duration")),
        "content": extracted["content"] or extracted["raw_content"],
        "thinking": thinking_text,
    }

    for key in [
        "created_at",
        "done",
        "done_reason",
        "prompt_eval_count",
        "eval_count",
        "total_duration_s",
        "load_duration_s",
        "prompt_eval_duration_s",
        "eval_duration_s",
    ]:
        if top_level.get(key) is None:
            top_level.pop(key, None)

    if not top_level.get("content"):
        top_level.pop("content", None)

    return top_level
