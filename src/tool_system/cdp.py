"""Health check for an already-running Chrome CDP endpoint.

This module intentionally never launches Chrome or retries indefinitely.
"""

from __future__ import annotations

import json
from urllib.error import URLError
from urllib.request import urlopen


DEFAULT_CDP_VERSION_URL = "http://127.0.0.1:9222/json/version"


def check_cdp(url: str = DEFAULT_CDP_VERSION_URL, timeout: float = 3.0) -> dict[str, str]:
    """Return verified browser metadata or raise a clear connection error.

    This function only reads the existing endpoint. It does not create a browser,
    start a process, or perform any UI actions.
    """
    try:
        with urlopen(url, timeout=timeout) as response:
            if response.status != 200:
                raise RuntimeError(f"CDP endpoint returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError) as exc:
        raise ConnectionError(
            f"Existing Chrome CDP endpoint is unreachable at {url}; Chrome was not started."
        ) from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("CDP endpoint returned invalid JSON.") from exc

    browser = payload.get("Browser")
    websocket = payload.get("webSocketDebuggerUrl")
    if not isinstance(browser, str) or not browser:
        raise RuntimeError("CDP response is missing the Browser field.")
    if not isinstance(websocket, str) or not websocket.startswith("ws"):
        raise RuntimeError("CDP response is missing a valid webSocketDebuggerUrl.")

    return {"browser": browser, "webSocketDebuggerUrl": websocket}
