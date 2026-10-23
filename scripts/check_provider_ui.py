"""Real signed-in provider UI preflight on the existing ONE Kaggle Chrome.

Attaches to the already-running local CDP 9222 browser, creates one short-lived
tab per provider within its ONE existing Chrome context, reads DOM only,
and closes only the temporary tab. Never starts another browser, clicks,
sends prompts, logs cookies, reads passwords, or publishes anything.

A visible chat composer is evidence the UI loads, NOT proof of server-side
sign-in or Gemini's ability to analyze a video. Report that distinction.
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse
from typing import Any
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from money_ai.chrome import Chrome

PROFILE = "/kaggle/working/tool-system-v2-runtime/chrome_profile"
DOWNLOADS = "/kaggle/working/tool-system-v2-runtime/downloads"
PROVIDERS = (
    ("GEMINI", "https://gemini.google.com/app"),
    ("META", "https://www.meta.ai/"),
)
LOGIN_HOSTS = {
    "accounts.google.com", "accounts.google.com.vn",
    "login.facebook.com", "www.facebook.com",
}


def classify_view(page: Any, provider: str) -> str:
    """Conservative evidence levels; never conflate loaded UI with login."""
    url = page.url
    host = (urlparse(url).hostname or "").lower()
    if host in LOGIN_HOSTS or "signin" in url.lower() or "login" in url.lower():
        return "OWNER_LOGIN_REQUIRED"
    if page.locator('input[type="password"]').count():
        return "OWNER_LOGIN_REQUIRED"
    expected = "gemini.google.com" if provider == "GEMINI" else "meta.ai"
    if host not in {expected, "www." + expected}:
        return "UNEXPECTED_PAGE"
    # Presence alone does not prove account authorization; user may be a guest.
    selectors = (
        'textarea',
        '[contenteditable="true"]',
        '[role="textbox"]',
    )
    for selector in selectors:
        try:
            if page.locator(selector).count() > 0:
                return "CHAT_UI_AVAILABLE_LOGIN_UNVERIFIED"
        except Exception:
            continue
    return "PAGE_LOADED_COMPOSER_UNCONFIRMED"


def probe_provider(context: Any, provider: str, url: str) -> str:
    """A temporary tab in the SAME browser, never reuses/closes owner tabs."""
    tab = context.new_page()
    try:
        tab.goto(url, wait_until="domcontentloaded", timeout=45000)
        tab.wait_for_timeout(2000)
        return classify_view(tab, provider)
    except Exception as exc:
        return "READ_ONLY_CHECK_FAILED_" + type(exc).__name__
    finally:
        tab.close()


def main() -> dict[str, str]:
    if os.environ.get("DISPLAY") != ":99":
        # Pass DISPLAY explicitly rather than assume Notebook environment.
        os.environ["DISPLAY"] = ":99"
    with Chrome(profile_dir=PROFILE, downloads_dir=DOWNLOADS,
                cdp_url="http://127.0.0.1:9222") as browser:
        assert browser._shared_browser is not None, "CHROME_CDP_NOT_ATTACHED"
        ctx = browser.context
        if ctx is None:
            raise RuntimeError("CHROME_CONTEXT_NOT_AVAILABLE")
        results: dict[str, str] = {}
        for name, url in PROVIDERS:
            results[name] = probe_provider(ctx, name, url)
            print(f"TOOL_SYSTEM_{name}_UI={results[name]}", flush=True)
    print("TOOL_SYSTEM_EXISTING_CHROME_REUSED=PASS", flush=True)
    print("TOOL_SYSTEM_OWNER_LOGIN=OWNER_REPORTED_NOT_INDEPENDENTLY_VERIFIED", flush=True)
    print("TOOL_SYSTEM_BROWSER_AI_ACTION=NOT_YET_VERIFIED", flush=True)
    return results


if __name__ == "__main__":
    main()
