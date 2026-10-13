"""Test several current OpenRouter free text models for usable replies.

Save this script in the ResolveOS backend folder and run:
    python .\resolveos_openrouter_check_v2.py

The script reads backend/.env, never prints the API key, and does not edit files.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from dotenv import load_dotenv
except ImportError:
    print("Missing python-dotenv. Install it with: python -m pip install python-dotenv")
    sys.exit(1)

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH, override=True)
API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()

# Current free text-model IDs listed by OpenRouter. Availability can change.
CANDIDATES = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "poolside/laguna-s-2.1:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "openrouter/free",
]


def to_text(content: object) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces = []
        for part in content:
            if isinstance(part, dict) and isinstance(part.get("text"), str):
                pieces.append(part["text"])
        return "\n".join(pieces).strip()
    return ""


def call_model(model: str) -> tuple[int | None, str, str]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a helpful text assistant. Answer the user in plain text."},
            {"role": "user", "content": "Reply with exactly this text: ResolveOS AI connection works."},
        ],
        "max_tokens": 100,
        "temperature": 0,
        "stream": False,
    }
    req = Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "http://127.0.0.1:8000",
            "X-OpenRouter-Title": "ResolveOS connectivity check",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=45) as response:
            status = response.status
            data = json.loads(response.read().decode("utf-8", errors="replace"))
        choices = data.get("choices") or []
        message = choices[0].get("message") or {} if choices else {}
        content = to_text(message.get("content"))
        finish_reason = str(choices[0].get("finish_reason", "unknown")) if choices else "no_choices"
        actual_model = str(data.get("model") or model)
        detail = f"response_model={actual_model}; finish_reason={finish_reason}"
        return status, content, detail
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = "(no error body)"
        # Error bodies should not contain the API key, but avoid printing excessive content.
        return exc.code, "", body[:500].replace(API_KEY, "[REDACTED]")
    except (URLError, TimeoutError) as exc:
        return None, "", f"network_error={type(exc).__name__}: {exc}"
    except (ValueError, KeyError, TypeError) as exc:
        return None, "", f"response_parse_error={type(exc).__name__}: {exc}"


def main() -> int:
    print(f"Environment file found: {ENV_PATH.exists()}")
    print(f"OpenRouter API key configured: {bool(API_KEY)}")
    if not API_KEY:
        print("OPENROUTER_API_KEY is empty in backend\\.env. Add a current key locally, then retry.")
        return 2

    for model in CANDIDATES:
        print(f"\nTesting model: {model}")
        status, content, detail = call_model(model)
        print(f"HTTP status: {status if status is not None else 'network error'}")
        print(f"Details: {detail}")
        if status == 200 and content:
            print(f"Model reply: {content[:300]}")
            print("\nSUCCESS: This model returned usable text.")
            print("Set the following line in backend\\.env:")
            print(f"OPENROUTER_MODEL={model}")
            return 0
        if content:
            print(f"Reply (not usable as success): {content[:200]}")
        else:
            print("Model returned no text; trying the next model.")

    print("\nNo candidate returned usable text.")
    print("Check the key/account status at https://openrouter.ai/settings/keys and try again later.")
    print("Do not paste your key into chat when asking for help.")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
