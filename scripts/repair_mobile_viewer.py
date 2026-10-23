"""One safe mobile noVNC repair and end-to-end local diagnostics.

It does NOT launch or restart Chrome, VNC, websockify, Jupyter, or Kaggle.
It updates only noVNC's proxy-path JavaScript and uses real loopback sockets
to verify the RFB greeting and WebSocket Upgrade. Remote gateway not verified.
"""
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main():
    runpy.run_path(str(ROOT / "scripts" / "fix_novnc_kaggle_proxy.py"),
                   run_name="__main__")
    module = runpy.run_path(str(ROOT / "scripts" / "diagnose_novnc.py"))
    results = module["diagnostic"]()
    # Do not confuse live mobile connectivity with local backend validation.
    if (results["CHROME_CDP"] == "PASS" and
        results["X11VNC_RFB"] == "PASS" and
        results["LOCAL_WEBSOCKET"] == "PASS" and
        results["NOVNC_HTTP"] == "PASS" and
        results["PROXY_JS"] == "PASS"):
        print("TOOL_SYSTEM_MOBILE_BACKEND_LOCAL_PASS")
        print("Refresh the EXISTING noVNC tab (not Kaggle/Chrome).")
    else:
        print("TOOL_SYSTEM_MOBILE_BACKEND_LOCAL_FAIL: no proxy restart attempted")
        print("Do not restart Chrome; inspect the printed diagnostic results.")


if __name__ == "__main__":
    main()
