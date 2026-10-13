"""Safely test the OpenRouter connection used by ResolveOS.

Run from the backend folder with the project's virtual environment active:
    python .\resolveos_openrouter_check.py

This script never prints the API key. It sends a small test request to OpenRouter.
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
    print("Missing dependency: python-dotenv. Install with: python -m pip install python-dotenv")
    sys.exit(1)

ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(ENV_PATH, override=True)

api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
model = os.getenv("OPENROUTER_MODEL", "openrouter/free").strip() or "openrouter/free"

print(f"Environment file found: {ENV_PATH.exists()}")
print(f"OpenRouter API key configured: {bool(api_key)}")
print(f"Selected model: {model}")

if not api_key:
    print("Result: OPENROUTER_API_KEY is missing or empty in backend\\.env")
    sys.exit(2)

payload = {
    "model": model,
    "messages": [
        {"role": "system", "content": "Reply briefly to API connection tests."},
        {"role": "user", "content": "Reply with exactly: ResolveOS OpenRouter connection works."},
    ],
    "max_tokens": 40,
}
request = Request(
    "https://openrouter.ai/api/v1/chat/completions",
    data=json.dumps(payload).encode("utf-8"),
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://127.0.0.1:8000",
        "X-Title": "ResolveOS local connectivity check",
    },
    method="POST",
)

try:
    with urlopen(request, timeout=30) as response:
        data = json.loads(response.read().decode("utf-8"))
        status = response.status
    content = ""
    choices = data.get("choices") or []
    if choices:
        content = (choices[0].get("message") or {}).get("content") or ""
    print(f"HTTP status: {status}")
    print(f"Response model: {data.get('model', 'not specified')}")
    print(f"Model reply: {content[:500] if content else '(empty response)'}")
    if status == 200 and content:
        print("Result: OpenRouter connection works.")
    else:
        print("Result: Request returned HTTP 200 but no usable text reply. Check model availability.")
        sys.exit(3)
except HTTPError as exc:
    # Safe to print the server's error body; never print request headers or the API key.
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:
        body = "(no error details returned)"
    print(f"HTTP error: {exc.code}")
    print(f"OpenRouter response: {body[:1500]}")
    if exc.code == 401:
        print("Check that the key in backend\\.env is a valid, active OpenRouter API key.")
    elif exc.code == 402:
        print("The account/model may require credits or have no remaining free quota.")
    elif exc.code == 429:
        print("Rate limit or free-model capacity was reached; retry later or choose another available model.")
    sys.exit(4)
except URLError as exc:
    print(f"Network error: {exc.reason}")
    print("Check your internet connection and try again.")
    sys.exit(5)
except TimeoutError:
    print("Request timed out. Check connectivity and retry later.")
    sys.exit(6)
except Exception as exc:
    print(f"Unexpected error: {type(exc).__name__}: {exc}")
    sys.exit(7)
