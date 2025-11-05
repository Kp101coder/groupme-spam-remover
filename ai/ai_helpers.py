import ollama
from typing import List, Optional

# AI model config - kept inside this module since helpers operate on the model
MODEL = "deepseek-r1:14b"
ollama_model = ollama.Client()


def check_model_availability() -> bool:
    """Return True if model is available locally (may raise on client errors)."""
    models = ollama_model.list()
    available_models = [m.get("model") for m in models.get("models", [])]
    return MODEL in available_models


def pull_model() -> None:
    """Pull the configured model. Caller should handle exceptions."""
    ollama_model.pull(MODEL)


def set_model(model_name: str) -> None:
    """Update the module model name and reinitialize the client."""
    global MODEL, ollama_model
    MODEL = model_name
    ollama_model = ollama.Client()


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
    ollama_model.remove(name)


def prompt(message: str, system_message: str, data: List[dict] = None, train_start: Optional[str] = None, train_end: Optional[str] = None, think: bool = False) -> Optional[str]:
    """Send a prompt to the model. Raises on client errors; caller must catch/log.

    This function is a direct copy of the server prompt logic but doesn't swallow
    exceptions. It returns the final response content string.
    """
    messages = []

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
    # response structure expected to contain ['message']['content']
    if isinstance(response, dict) and 'message' in response and 'content' in response['message']:
        response_content = response['message']['content']
    else:
        # Fallback to string representation
        return str(response)

    if not think and "</think>" in response_content:
        # Strip any think tags and return everything after the final </think>
        response_content = response_content[response_content.find("</think>") + 8:].strip()

    return response_content
