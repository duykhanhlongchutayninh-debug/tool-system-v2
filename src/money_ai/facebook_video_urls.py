"""Validate actual Facebook video post permalinks before any media extractor.

A Page's generic /videos/ navigation link is NOT a video source. Use a
machine-readable video ID, or report that the original link is unverified.
No cookies, HTTP request, or browser needed.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit, parse_qs

FB_HOSTS = frozenset({"www.facebook.com", "facebook.com", "m.facebook.com"})
ID = re.compile(r"[0-9]{5,30}\Z")


@dataclass(frozen=True)
class VideoLink:
    kind: str
    video_url: str | None


def classify_video_link(url: str) -> VideoLink:
    """Keep only explicit numeric video post ID URLs.

    Video hubs and Facebook share/redirect short codes need browser
    resolution; they are not valid inputs to an MP4 downloader.
    """
    if not isinstance(url, str):
        return VideoLink("INVALID_URL", None)
    try:
        u = urlsplit(url)
    except ValueError:
        return VideoLink("INVALID_URL", None)
    if (u.scheme != "https" or (u.hostname or "").lower() not in FB_HOSTS
            or u.username or u.password):
        return VideoLink("NOT_FACEBOOK", None)
    parts = [x for x in u.path.split("/") if x]
    if not parts:
        return VideoLink("PAGE_HOME", None)
    if len(parts) == 2 and parts[0].lower() == "reel" and ID.fullmatch(parts[1]):
        return VideoLink("REEL_POST", f"https://www.facebook.com/reel/{parts[1]}")
    if len(parts) == 1 and parts[0].lower() == "watch":
        video_id = parse_qs(u.query).get("v", [""])[0]
        if ID.fullmatch(video_id):
            return VideoLink("WATCH_POST", f"https://www.facebook.com/watch/?v={video_id}")
        return VideoLink("WATCH_SECTION", None)
    if "videos" in [x.lower() for x in parts]:
        index = [x.lower() for x in parts].index("videos")
        # /videos/, /Page/videos/ and /Page/videos/live are sections.
        if len(parts) > index + 1 and ID.fullmatch(parts[-1]):
            return VideoLink("VIDEO_POST", f"https://www.facebook.com/watch/?v={parts[-1]}")
        return VideoLink("VIDEOS_SECTION_OR_NON_ID", None)
    if parts[0].lower() in {"share", "reels", "watch", "videos"}:
        return VideoLink("SHARE_OR_VIDEO_SECTION", None)
    return VideoLink("OTHER_FACEBOOK_LINK", None)
