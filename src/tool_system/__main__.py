"""Command-line entry point for safe, read-only diagnostics."""

import argparse
import json
import sys

from .cdp import check_cdp


def main() -> int:
    parser = argparse.ArgumentParser(prog="tool-system")
    parser.add_argument("command", choices=["cdp-check"])
    args = parser.parse_args()

    if args.command == "cdp-check":
        try:
            result = check_cdp()
        except (ConnectionError, RuntimeError) as exc:
            print(json.dumps({"status": "FAILED", "error": str(exc)}, ensure_ascii=False))
            return 1
        print(json.dumps({"status": "PASS", **result}, ensure_ascii=False))
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
