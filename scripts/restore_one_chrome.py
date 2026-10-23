"""Kaggle recovery entrypoint: one Chrome + external read-only CDP proof.

Run from a NEW Notebook cell with runpy.run_path(...). Uses the one already
running Kaggle CPU and no additional service. Does not re-run GitHub bootstrap,
change credentials, touch Supabase, or launch a second Chrome.
"""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Jupyter imported scripts.kaggle_cpu_desktop during an earlier failed launch.
# Loading it through the module import cache would reuse the OLD xdpyinfo code
# even after the auto-updater switches the GitHub release symlink. Execute the
# launcher from the currently active source file on EACH retry instead.
launcher = runpy.run_path(str(ROOT / "scripts" / "kaggle_cpu_desktop.py"))
start = launcher["start"]


def _cookie_artifact_counts(profile: Path) -> tuple[int, int, int]:
    """Count unexpired domain cookies without reading names or secret values.

    These counts are diagnostic artifacts only; they do NOT assert login.
    Chrome uses microseconds since 1601-01-01 for expires_utc.
    """
    folders = [profile / "Default"]
    try:
        folders.extend(p for p in profile.glob("Profile [1-9]*") if p.is_dir())
    except OSError:
        pass
    dbs = []
    for folder in folders:
        for path in (folder / "Network" / "Cookies", folder / "Cookies"):
            if path.is_file() and path not in dbs:
                dbs.append(path)

    now_chrome = int((time.time() + 11644473600) * 1_000_000)
    google_rows = 0
    meta_facebook_rows = 0
    read_dbs = 0
    for path in dbs:
        try:
            uri = path.resolve().as_uri() + "?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=1.0) as db:
                rows = db.execute(
                    "SELECT host_key, expires_utc FROM cookies"
                ).fetchall()
            read_dbs += 1
        except (OSError, sqlite3.Error, ValueError):
            continue
        for host, expires in rows:
            domain = str(host or "").lower().lstrip(".").rstrip(".")
            # Session cookies use expires_utc=0; persistent cookies count only
            # while their stored expiry is still in the future.
            try:
                valid = int(expires or 0) == 0 or int(expires) > now_chrome
            except (TypeError, ValueError):
                valid = False
            if not valid:
                continue
            if domain == "google.com" or domain.endswith(".google.com") or domain in {
                "googleapis.com", "googleusercontent.com"
            } or domain.endswith((".googleapis.com", ".googleusercontent.com")):
                google_rows += 1
            if any(domain == root or domain.endswith("." + root) for root in (
                "facebook.com", "fb.com", "meta.com", "messenger.com"
            )):
                meta_facebook_rows += 1
    return read_dbs, google_rows, meta_facebook_rows


def main() -> None:
    details = start()
    if details.get("cdp") != "http://127.0.0.1:9222":
        raise RuntimeError("SHARED_CHROME_CDP_UNEXPECTED")
    env = launcher["_safe_child_env"]()
    env["DISPLAY"] = ":99"
    env["MONEY_AI_CDP_URL"] = "http://127.0.0.1:9222"
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_shared_chrome.py")],
        env=env, capture_output=True, text=True,
        timeout=35, check=False,
    )
    if result.returncode or "MONEY_AI_SHARED_CHROME_CDP_PASS" not in result.stdout:
        raise RuntimeError("SHARED_CHROME_ATTACH_NOT_VERIFIED: inspect Kaggle logs, do not launch a second Chrome")
    os.environ["DISPLAY"] = ":99"
    os.environ["MONEY_AI_CDP_URL"] = "http://127.0.0.1:9222"
    print("MONEY_AI_SHARED_CHROME_CDP_PASS")
    print("ONE_CHROME_RESTORED")
    profile = Path(details["profile"])
    print("PROFILE:", profile)
    print("PROFILE_CLOUD_RESTORE:", details.get("profile_restore", "UNKNOWN"))
    print("CHROME_REUSED:", details["reused"])
    db_count, google_rows, meta_fb_rows = _cookie_artifact_counts(profile)
    print("MONEY_AI_COOKIE_AUDIT_DATABASES_READ=" + str(db_count), flush=True)
    print("MONEY_AI_COOKIE_AUDIT_GOOGLE_UNEXPIRED_ROWS=" + str(google_rows), flush=True)
    print("MONEY_AI_COOKIE_AUDIT_META_FACEBOOK_UNEXPIRED_ROWS=" + str(meta_fb_rows), flush=True)
    print("LOGIN_STATE=NOT_VERIFIED — cookie rows are not proof of an active login; inspect the visible existing Chrome")


def _failure_diagnostics(exc: Exception) -> None:
    """One concise error packet; owner never needs another manual log cell."""
    name = type(exc).__name__
    message = str(exc).replace("\n", " ")[:220]
    print("MONEY_AI_RECOVERY_FAILED:", name, message, flush=True)
    logdir = Path("/kaggle/working/money-ai/logs")
    if message.startswith("VIEWER_") or "NOVNC" in message or "VNC" in message:
        candidates = ("viewer_install.log", "vnc.log", "screen.log")
    elif "CHROME" in message or "CHROMIUM" in message:
        candidates = ("chrome_deps.log", "chrome.log")
    else:
        candidates = ("viewer_install.log", "chrome.log")
    for filename in candidates:
        path = logdir / filename
        if path.is_file():
            tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
            print("MONEY_AI_LOG", filename, flush=True)
            for line in tail:
                # These are package/server logs, never raw cookies or credentials.
                print(line[:220], flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _failure_diagnostics(exc)
        raise
