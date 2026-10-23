"""Start Tool System V2's Kaggle desktop: one Chrome, CDP, and noVNC screen."""
from __future__ import annotations
from pathlib import Path
import runpy

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "kaggle_cpu_desktop.py"

def main() -> None:
    if not Path("/kaggle").exists():
        raise RuntimeError("KAGGLE_ONLY: run this from a Kaggle notebook")
    launcher = runpy.run_path(str(LAUNCHER))
    result = launcher["start"]()
    if result.get("chrome") != "CDP_OK" or result.get("cdp") != "http://127.0.0.1:9222":
        raise RuntimeError("TOOL_SYSTEM_CHROME_CDP_NOT_VERIFIED")
    print("TOOL_SYSTEM_CHROME_CDP=PASS", flush=True)
    print("TOOL_SYSTEM_CHROME_REUSED=" + str(result.get("reused")), flush=True)
    print("TOOL_SYSTEM_DISPLAY=" + str(result.get("display")), flush=True)
    print("TOOL_SYSTEM_SCREEN_LOCAL=" + str(result.get("screen_local")), flush=True)
    print("TOOL_SYSTEM_SCREEN_PROXY_SUFFIX=" + str(result.get("screen_proxy_suffix")), flush=True)
    print("TOOL_SYSTEM_SCREEN_URL_TEMPLATE=https://kkb-production.jupyter-proxy.kaggle.net/k/<KERNEL_ID>/<SESSION_TOKEN>/proxy/proxy/6080/vnc.html", flush=True)
    print("TOOL_SYSTEM_SCREEN_STATUS=LOCAL_READY; authenticated Kaggle mobile route still needs verification", flush=True)

if __name__ == "__main__":
    main()
