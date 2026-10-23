"""ONE bounded owner-started action for first REAL source Reel.

From Kaggle Notebook using runpy:
1. Public yt-dlp extraction of ONLY confirmed video post ID(s) (max 3).
2. If that fails, read-only media inspection of the already-open ONE Chrome
   (no second browser, cookies, extra key, publication or fabricated MP4).
3. Report a clear honest milestone. Do not retry after failure automatically.

Existing video source is for inspiration/analysis; publication requires
appropriate permission and owner's final Telegram approval.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for p in [ROOT, ROOT / "src"]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from scripts import download_source_ytdlp as downloader
from scripts import probe_first_reel as reel_probe


def run() -> dict:
    print("TOOL_SYSTEM_FIRST_REEL_START", flush=True)
    print("Đang thử đúng bài Reel có ID, không lặp lại các link Facebook điều hướng.",
          flush=True)
    result = downloader.run()
    if result["status"] == "REAL_MP4_WITH_AUDIO_VERIFIED":
        print("TOOL_SYSTEM_FIRST_REEL=REAL_MP4_WITH_AUDIO_VERIFIED", flush=True)
        print("TOOL_SYSTEM_PUBLICATION_PERMISSION=NOT_VERIFIED", flush=True)
        return {"status": "REAL_MP4_WITH_AUDIO_VERIFIED",
                "source_report": str(downloader.REPORT)}
    print("TOOL_SYSTEM_FIRST_REEL_DOWNLOAD=" + result["status"], flush=True)
    print("Đang kiểm tra trình phát của chính Chrome Kaggle, không mở Chrome mới.",
          flush=True)
    try:
        reel_probe.main()
        print("TOOL_SYSTEM_FIRST_REEL=NOT_DOWNLOADED_BROWSER_PROBED", flush=True)
        return {"status": "NOT_DOWNLOADED_BROWSER_PROBED",
                "source_report": str(downloader.REPORT),
                "browser_report": str(reel_probe.REPORT)}
    except Exception as exc:
        print("TOOL_SYSTEM_FIRST_REEL=NOT_DOWNLOADED_PROBE_FAILED", flush=True)
        print("TOOL_SYSTEM_REEL_PROBE_ERROR=" + type(exc).__name__, flush=True)
        return {"status": "NOT_DOWNLOADED_PROBE_FAILED",
                "source_report": str(downloader.REPORT)}


if __name__ == "__main__":
    run()
