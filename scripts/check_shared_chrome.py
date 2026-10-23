"""Read-only Playwright attach proof; never navigates, launches or closes Chrome."""
from __future__ import annotations
import os
from playwright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"

def main() -> None:
    if os.environ.get("TOOL_SYSTEM_CDP_URL") != CDP:
        raise RuntimeError("TOOL_SYSTEM_CDP_URL_NOT_CONFIGURED")
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(CDP, timeout=10000)
        try:
            if len(browser.contexts) != 1:
                raise RuntimeError("TOOL_SYSTEM_EXPECTED_ONE_BROWSER_CONTEXT")
            pages = browser.contexts[0].pages
            print("TOOL_SYSTEM_CDP_PAGE_COUNT=" + str(len(pages)))
            print("TOOL_SYSTEM_SHARED_CHROME_CDP_PASS")
        finally:
            # Stop the driver connection; do not call browser.close().
            playwright.stop()

if __name__ == "__main__":
    main()
