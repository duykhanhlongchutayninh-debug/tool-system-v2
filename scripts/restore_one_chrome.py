"""Create/reuse Tool System V2's single Kaggle Chrome using the ported Money AI launcher.

The launcher refuses to create a second browser when any Chrome process already
exists without a known CDP endpoint. Uses a separate Tool System V2 local profile
and does not restore Money AI cookies or read its Supabase credentials.
"""
from __future__ import annotations
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "kaggle_cpu_desktop.py"

def main() -> None:
    if not Path("/kaggle").exists():
        raise RuntimeError("KAGGLE_ONLY")
    if not LAUNCHER.is_file():
        raise RuntimeError("TOOL_SYSTEM_CHROME_LAUNCHER_MISSING")
    launcher = runpy.run_path(str(LAUNCHER))
    result = launcher["start"]()
    if result.get("chrome") != "CDP_OK" or result.get("cdp") != "http://127.0.0.1:9222":
        raise RuntimeError("TOOL_SYSTEM_CHROME_START_NOT_VERIFIED")
    print("TOOL_SYSTEM_CHROME_CDP_PASS")
    print("TOOL_SYSTEM_CHROME_REUSED=" + str(bool(result.get("reused"))))
    print("TOOL_SYSTEM_PROFILE_PATH=" + str(result.get("profile")))
    print("TOOL_SYSTEM_PROFILE_LOGIN=NOT_VERIFIED")
    print("TOOL_SYSTEM_CHROME_READY")
    print("TOOL_SYSTEM_NOVNC_LOCAL=" + str(result.get("screen_local")))
    print("TOOL_SYSTEM_MOBILE_SCREEN=NOT_VERIFIED")

if __name__ == "__main__":
    main()
