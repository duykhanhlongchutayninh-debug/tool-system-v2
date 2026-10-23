"""Media QA: inspect actual MP4 bytes and emit stable, machine-readable failures."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import math
import subprocess


# Meta AI's browser UI has been observed to return a valid 10-second 720x1280
# MP4. Facebook's current help says Reels can be any length/orientation after
# its June 2025 video-to-Reels change. Keep a conservative 3-second technical
# floor for Meta drafts so they can reach the owner's Telegram review; maintain
# the existing >=30-second policy for Google Flow and all other providers.
MIN_META_VIDEO_SECONDS = 3.0
MIN_STANDARD_VIDEO_SECONDS = 30.0


def minimum_video_duration(provider: str | None) -> float:
    """Return one shared duration policy for probing, orchestration and state gates."""
    value = str(provider or "").strip().casefold()
    return MIN_META_VIDEO_SECONDS if value in {
        "meta ai", "meta", "meta.ai", "meta_ai",
    } else MIN_STANDARD_VIDEO_SECONDS


@dataclass(frozen=True)
class VideoProof:
    duration: float
    width: int
    height: int


def probe_mp4(path: str | Path, *, min_duration: float = MIN_STANDARD_VIDEO_SECONDS) -> VideoProof:
    """Probe file bytes; short Meta clips are allowed only under the Meta QA policy."""
    f = Path(path)
    try:
        minimum = float(min_duration)
    except (TypeError, ValueError):
        raise RuntimeError("VIDEO_MP4_MINIMUM_DURATION_INVALID") from None
    if not math.isfinite(minimum) or minimum < 0:
        raise RuntimeError("VIDEO_MP4_MINIMUM_DURATION_INVALID")
    if f.suffix.lower() != ".mp4" or not f.is_file() or f.stat().st_size < 100:
        raise RuntimeError("VIDEO_MP4_FILE_MISSING_OR_EMPTY")
    try:
        p = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height", "-of", "json", str(f)],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except FileNotFoundError:
        raise RuntimeError("VIDEO_MP4_FFPROBE_NOT_INSTALLED") from None
    except subprocess.CalledProcessError:
        raise RuntimeError("VIDEO_MP4_FFPROBE_FAILED") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError("VIDEO_MP4_FFPROBE_TIMEOUT") from None
    try:
        data = json.loads(p.stdout)
        videos = [stream for stream in data.get("streams", [])
                  if stream.get("codec_type") == "video"]
        duration = float(data.get("format", {}).get("duration", 0))
        if not videos:
            raise RuntimeError("VIDEO_MP4_NO_VIDEO_STREAM")
        stream = videos[0]
        width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
    except RuntimeError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, AttributeError):
        raise RuntimeError("VIDEO_MP4_FFPROBE_OUTPUT_INVALID") from None
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("VIDEO_MP4_DURATION_INVALID")
    print(
        "TOOL_SYSTEM_VIDEO_PROBE=OBSERVED"
        + ";duration_seconds=" + f"{duration:.3f}"
        + ";minimum_seconds=" + f"{minimum:.3f}"
        + ";width=" + str(width)
        + ";height=" + str(height),
        flush=True,
    )
    if duration < minimum:
        raise RuntimeError(
            "VIDEO_MP4_DURATION_BELOW_MINIMUM"
            + f": actual={duration:.3f}s; required={minimum:.3f}s"
        )
    if height <= 0 or width <= 0 or not 0.5 < width / height < 0.65:
        raise RuntimeError(
            "VIDEO_MP4_ASPECT_RATIO_INVALID"
            + f": width={width}; height={height}; required=portrait_9_16"
        )
    print("TOOL_SYSTEM_VIDEO_PROBE=PASS", flush=True)
    return VideoProof(duration, width, height)
