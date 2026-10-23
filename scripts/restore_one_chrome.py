"""Start/reuse Tool System V2's single Kaggle Chrome and verify read-only CDP attach.

No provider automation, account actions, or publishing. This uses only a local
Tool System V2 profile; it does not read Money AI's secrets or Supabase.
"""
from __future__ import annotations
import os
from pathlib import Path
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "kaggle_cpu_desktop.py"

def main() -> None:
    if not LAUNCHER.is_file():
        raise RuntimeError("TOOL_SYSTEM_CHROME_LAUNCHER_MISSING")
    launcher = runpy.run_path(str(LAUNCHER))
    details = launcher["start"]()
    if details.get("cdp") != "http://127.0.0.1:9222":
        raise RuntimeError("TOOL_SYSTEM_CDP_ENDPOINT_UNEXPECTED")
    env = launcher["_safe_child_env"]()
    env["DISPLAY"] = ":99"
    env["TOOL_SYSTEM_CDP_URL"] = "http://127.0.0.1:9222"
    env["PYTHONPATH"] = str(ROOT / "src")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_shared_chrome.py")],
        env=env, capture_output=True, text=True, timeout=35, check=False,
    )
    if result.returncode or "TOOL_SYSTEM_SHARED_CHROME_CDP_PASS" not in result.stdout:
        # Do not print arbitrary browser/account content from stderr/stdout.
        tail = "\n".join((result.stdout + "\n" + result.stderr).splitlines()[-8:])
        raise RuntimeError("TOOL_SYSTEM_SHARED_CHROME_ATTACH_NOT_VERIFIED:\n" + tail[:1400])
    print("TOOL_SYSTEM_SHARED_CHROME_CDP_PASS")
    print("TOOL_SYSTEM_CHROME_REUSED=" + str(bool(details.get("reused"))))
    print("TOOL_SYSTEM_PROFILE_PATH=" + str(details.get("profile")))
    print("TOOL_SYSTEM_PROFILE_LOGIN=NOT_VERIFIED")
    print("TOOL_SYSTEM_CHROME_READY")

if __name__ == "__main__":
    main()
