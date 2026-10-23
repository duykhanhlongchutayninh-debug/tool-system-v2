"""Read hashtags from exactly the selected Facebook Reel via ONE owner Chrome.

Child process exists only to avoid the Kaggle Jupyter asyncio/sync-Playwright
conflict. It ATTACHES to the already running CDP 9222; creates a temporary
tab, reads only own Reel metadata and visible caption, closes that tab.
It does NOT download content, export cookies, click Publish or log captions.
"""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "src"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from money_ai.chrome import Chrome
from money_ai.facebook_video_urls import classify_video_link
from money_ai.source_step05 import (
    CAPTION_PATH, SOURCE_CANONICAL, SHA, tags_from_caption
)

SELECTORS = (
    ('meta[property="og:description"]', "FACEBOOK_REEL_OG_DESCRIPTION"),
    ('meta[name="description"]', "FACEBOOK_REEL_META_DESCRIPTION"),
    ('[role="main"] [data-ad-preview="message"]', "FACEBOOK_REEL_VISIBLE_CAPTION"),
    ('[role="main"] [data-ad-comet-preview="message"]',
     "FACEBOOK_REEL_VISIBLE_CAPTION"),
    ('[role="main"] [data-testid="post_message"]',
     "FACEBOOK_REEL_VISIBLE_CAPTION"),
)

def capture(tab, reel_url: str, digest: str) -> dict:
    requested = classify_video_link(reel_url)
    if requested.kind != "REEL_POST" or not SHA.fullmatch(digest):
        raise RuntimeError("SOURCE_REEL_OR_SHA_INVALID")
    tab.goto(requested.video_url, wait_until="domcontentloaded", timeout=35000)
    tab.wait_for_timeout(2400)
    actual = classify_video_link(tab.url)
    if (actual.video_url != requested.video_url
            or tab.locator('input[type="password"]').count()):
        raise RuntimeError("SOURCE_REEL_URL_NOT_CONFIRMED_OR_LOGIN_REQUIRED")
    # Meta tags/visible caption from THIS specific post only. Never scrape
    # comments, hashtags guessed by a model, sidebar or page description.
    no_tag_candidate = None
    for selector, provenance in SELECTORS:
        try:
            locator = tab.locator(selector).first
            text = (locator.get_attribute("content", timeout=2000)
                    if selector.startswith("meta[")
                    else locator.inner_text(timeout=2000))
        except Exception:
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        text = text.strip()[:6000]
        tags = tags_from_caption(text)
        if not tags:
            no_tag_candidate = no_tag_candidate or (text, provenance)
            continue
        return {
            "source_page": SOURCE_CANONICAL,
            "source_reel_url": requested.video_url,
            "source_sha256": digest,
            "status": "SOURCE_HASHTAGS_CAPTURED",
            "metadata_source": provenance,
            "caption_text": text,
            "caption_sha256": sha256(text.encode()).hexdigest(),
            "hashtags": tags,
        }
    if no_tag_candidate is None:
        raise RuntimeError("SOURCE_CAPTION_UNAVAILABLE")
    # A readable post description with no hashtags is evidence for an empty
    # hashtag caption; report that distinction, don't generate substitute tags.
    text, provenance = no_tag_candidate
    return {
        "source_page": SOURCE_CANONICAL,
        "source_reel_url": requested.video_url,
        "source_sha256": digest,
        "status": "SOURCE_CAPTION_NO_HASHTAGS",
        "metadata_source": provenance,
        "caption_text": text,
        "caption_sha256": sha256(text.encode()).hexdigest(),
        "hashtags": [],
    }


def run(reel_url: str, digest: str) -> dict:
    os.environ["DISPLAY"] = ":99"
    with Chrome("/kaggle/working/tool-system-v2-runtime/chrome_profile",
                "/kaggle/working/tool-system-v2-runtime/downloads",
                cdp_url="http://127.0.0.1:9222") as browser:
        if browser._shared_browser is None or browser.context is None:
            raise RuntimeError("STEP05_SHARED_CHROME_NOT_ATTACHED")
        tab = browser.context.new_page()
        try:
            result = capture(tab, reel_url, digest)
        finally:
            tab.close()
    CAPTION_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CAPTION_PATH.with_suffix(".partial")
    temp.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    temp.replace(CAPTION_PATH)
    print("TOOL_SYSTEM_SOURCE_HASHTAGS=" + result["status"], flush=True)
    print("TOOL_SYSTEM_SOURCE_HASHTAG_COUNT=" + str(len(result["hashtags"])),
          flush=True)
    print("TOOL_SYSTEM_SOURCE_CAPTION_EVIDENCE=" + str(CAPTION_PATH), flush=True)
    return result


if __name__ == "__main__":
    # Called by the parent Tools.collect only; never Jupyter runpy directly.
    if len(sys.argv) != 3:
        raise SystemExit("STEP05_EXPECTS_REEL_AND_SOURCE_SHA")
    try:
        run(sys.argv[1], sys.argv[2])
    except Exception as exc:
        print("TOOL_SYSTEM_SOURCE_CAPTION_ERROR=" + type(exc).__name__, flush=True)
        raise SystemExit(1) from None
