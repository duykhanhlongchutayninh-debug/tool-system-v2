import json
import unittest
from unittest.mock import patch

from tool_system.cdp import check_cdp


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class CdpTests(unittest.TestCase):
    @patch("tool_system.cdp.urlopen")
    def test_valid_existing_endpoint(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse({
            "Browser": "Chrome/test",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/test",
        })
        result = check_cdp()
        self.assertEqual(result["browser"], "Chrome/test")
        mock_urlopen.assert_called_once()

    @patch("tool_system.cdp.urlopen")
    def test_missing_browser_field_fails(self, mock_urlopen):
        mock_urlopen.return_value = FakeResponse({
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/test",
        })
        with self.assertRaisesRegex(RuntimeError, "Browser field"):
            check_cdp()

    @patch("tool_system.cdp.urlopen")
    def test_unreachable_endpoint_is_reported_without_launching_chrome(self, mock_urlopen):
        from urllib.error import URLError
        mock_urlopen.side_effect = URLError("connection refused")
        with self.assertRaisesRegex(ConnectionError, "Chrome was not started"):
            check_cdp()
        mock_urlopen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
