"""Attach-only verification for an already-running Kaggle Chrome.

This script NEVER starts Chrome, Xvfb, VNC, or a new profile. Run it only
inside the same Kaggle kernel/session that already owns CDP port 9222.
"""
from __future__ import annotations
import os
from pathlib import Path
from playwright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"

def main() -> None:
    if not Path("/kaggle").exists():
        raise RuntimeError("KAGGLE_ONLY")
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(CDP, timeout=5000)
            try:
                if not browser.contexts:
                    raise RuntimeError("CDP_CONNECTED_BUT_NO_BROWSER_CONTEXT")
                pages = browser.contexts[0].pages
                print("TOOL_SYSTEM_SHARED_CHROME_CDP_PASS")
                print("TOOL_SYSTEM_CHROME_REUSED=True")
                print("TOOL_SYSTEM_PAGE_COUNT=" + str(len(pages)))
                print("TOOL_SYSTEM_LOGIN=NOT_VERIFIED")
            finally:
                # Disconnect the Playwright client; do not close browser.
                playwright.stop()
    except Exception as exc:
        print("TOOL_SYSTEM_SHARED_CHROME_CDP_FAIL")
        print("REASON=" + type(exc).__name__ + ": " + str(exc))
        raise

if __name__ == "__main__":
    main()
