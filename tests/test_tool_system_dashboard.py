import unittest

from scripts.tool_system_dashboard import _screen_url, _screen_url_from_server_base


class KaggleScreenUrlTests(unittest.TestCase):
    def test_builds_authenticated_screen_route(self):
        source = "https://kkb-production.jupyter-proxy.kaggle.net/k/kernel-123/token-abc/proxy"
        self.assertEqual(
            _screen_url(source),
            "https://kkb-production.jupyter-proxy.kaggle.net/k/kernel-123/token-abc/proxy/proxy/6080/vnc.html",
        )

    def test_accepts_jupyter_server_relative_base(self):
        self.assertEqual(
            _screen_url_from_server_base("/k/kernel-123/token-abc/proxy/"),
            "https://kkb-production.jupyter-proxy.kaggle.net/k/kernel-123/token-abc/proxy/proxy/6080/vnc.html",
        )

    def test_rejects_non_kaggle_host(self):
        with self.assertRaises(ValueError):
            _screen_url("https://attacker.example/k/kernel-123/token-abc/proxy")

    def test_rejects_query_token_copy(self):
        with self.assertRaises(ValueError):
            _screen_url("https://kkb-production.jupyter-proxy.kaggle.net/k/kernel-123/token-abc/proxy?leak=1")


if __name__ == "__main__":
    unittest.main()
