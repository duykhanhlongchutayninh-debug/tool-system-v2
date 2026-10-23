"""One-shot OFFLINE diagnosis for old Tool System V2 Facebook source candidate links.

Reads the JSON report already saved by Kaggle. No browser, no yt-dlp,
network requests, cookies, or second Chrome. Prints only safe URL shapes,
never source URLs, numeric video IDs or credentials.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from money_ai.facebook_video_urls import classify_video_link

REPORT = Path("/kaggle/working/tool-system-v2-runtime/source_inspection.json")


def diagnose(report: Path = REPORT) -> dict[str, object]:
    payload = json.loads(report.read_text(encoding="utf-8"))
    if payload.get("source_page") != "https://www.facebook.com/share/1AJ3zx9Uke/":
        raise RuntimeError("SOURCE_REPORT_DOES_NOT_MATCH_APPROVED_PAGE")
    records = payload.get("candidates") or []
    categories = Counter()
    valid = 0
    for item in records:
        if not isinstance(item, dict):
            categories["MALFORMED_RECORD"] += 1
            continue
        classified = classify_video_link(item.get("url"))
        categories[classified.kind] += 1
        if classified.video_url is not None:
            valid += 1
    result = {
        "cached_candidates": len(records),
        "genuine_post_link_count": valid,
        "generic_or_other_link_count": len(records) - valid,
        "by_link_type": dict(sorted(categories.items())),
        "source_mp4_downloaded": False,
    }
    print("TOOL_SYSTEM_LINKS_CACHED=" + str(result["cached_candidates"]))
    print("TOOL_SYSTEM_GENUINE_POST_LINKS=" + str(valid))
    print("TOOL_SYSTEM_NONVIDEO_NAVIGATION_LINKS=" + str(len(records) - valid))
    for kind, count in result["by_link_type"].items():
        print("TOOL_SYSTEM_LINK_TYPE_" + kind + "=" + str(count))
    print("TOOL_SYSTEM_SOURCE_MP4=NOT_DOWNLOADED")
    return result


if __name__ == "__main__":
    diagnose()
