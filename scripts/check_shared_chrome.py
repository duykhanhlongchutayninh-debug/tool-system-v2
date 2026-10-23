"""Read-only connection proof against the shared Kaggle CDP Chrome.

Run in a separate Python process: Jupyter's active async event loop cannot
invoke the Playwright sync API directly. Never create or navigate a browser.
"""
from __future__ import annotations
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from money_ai.chrome import Chrome


def check() -> None:
    if os.environ.get("MONEY_AI_CDP_URL") != "http://127.0.0.1:9222":
        raise RuntimeError("MONEY_AI_CDP_URL_NOT_CONFIGURED")
    if os.environ.get("DISPLAY") != ":99":
        raise RuntimeError("MONEY_AI_DISPLAY_NOT_CONFIGURED")
    # The constructor does not launch Chrome. The context manager connects
    # over CDP only when given the local loopback endpoint.
    with Chrome("/kaggle/working/money_ai_chrome_profile",
                "/kaggle/working/money-ai/downloads",
                cdp_url="http://127.0.0.1:9222") as connected:
        if connected._shared_browser is None or connected.page is None:
            raise RuntimeError("CHROME_CDP_ATTACHED_PAGE_MISSING")
        # Inspect an existing page without printing account content.
        if not isinstance(connected.page.url, str):
            raise RuntimeError("CHROME_PAGE_INVALID")
        print("MONEY_AI_SHARED_CHROME_CDP_PASS", flush=True)


if __name__ == "__main__":
    check()
