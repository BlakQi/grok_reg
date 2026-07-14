import unittest

from proxy_runtime import (
    ProxyRuntimeError,
    _build_forward_proxy_config,
    _build_sing_box_config,
    clear_thread_proxy_selection,
    proxy_candidates,
    resolve_config_proxy,
    resolve_proxy_url,
)


NODE = (
    "vless://00000000-0000-4000-8000-000000000001@192.0.2.1:2053"
    "?security=tls&type=ws&host=proxy.example.com&fp=chrome"
    "&sni=proxy.example.com&path=%2F&encryption=none#node"
)


class ProxyRuntimeTests(unittest.TestCase):
    def tearDown(self):
        clear_thread_proxy_selection()

    def test_non_vless_proxy_passes_through(self):
        self.assertEqual(resolve_proxy_url("http://127.0.0.1:7890"), "http://127.0.0.1:7890")

    def test_vless_ws_tls_mapping(self):
        config = _build_sing_box_config(NODE, 23123)
        inbound = config["inbounds"][0]
        outbound = config["outbounds"][0]

        self.assertEqual(inbound["listen"], "127.0.0.1")
        self.assertEqual(inbound["listen_port"], 23123)
        self.assertEqual(outbound["server"], "192.0.2.1")
        self.assertEqual(outbound["server_port"], 2053)
        self.assertEqual(outbound["transport"]["type"], "ws")
        self.assertEqual(outbound["transport"]["path"], "/")
        self.assertEqual(outbound["transport"]["headers"]["Host"], "proxy.example.com")
        self.assertEqual(outbound["tls"]["server_name"], "proxy.example.com")
        self.assertEqual(outbound["tls"]["utls"]["fingerprint"], "chrome")

    def test_invalid_vless_url_is_rejected(self):
        with self.assertRaises(ProxyRuntimeError):
            _build_sing_box_config("vless://missing-host", 23123)

    def test_authenticated_http_proxy_mapping(self):
        config = _build_forward_proxy_config(
            "http://user%40name:pass%3Aword@192.0.2.10:3129", 23124
        )
        outbound = config["outbounds"][0]
        self.assertEqual(outbound["type"], "http")
        self.assertEqual(outbound["server"], "192.0.2.10")
        self.assertEqual(outbound["server_port"], 3129)
        self.assertEqual(outbound["username"], "user@name")
        self.assertEqual(outbound["password"], "pass:word")

    def test_newline_proxy_pool_string(self):
        cfg = {"proxies": "http://first.invalid:7001\n\nhttp://second.invalid:7002\n"}
        self.assertEqual(
            proxy_candidates(cfg),
            ["http://first.invalid:7001", "http://second.invalid:7002"],
        )

    def test_empty_config_means_direct_connection(self):
        self.assertEqual(proxy_candidates({"proxy": "", "proxies": []}), [])
        self.assertEqual(resolve_config_proxy({"proxy": "", "proxies": []}), "")

    def test_pool_overrides_single_proxy_and_rotates_per_account(self):
        cfg = {
            "proxy": "http://legacy.invalid:7000",
            "proxies": ["http://first.invalid:7001", "socks5://second.invalid:7002"],
        }
        self.assertEqual(resolve_config_proxy(cfg), "http://first.invalid:7001")
        self.assertEqual(resolve_config_proxy(cfg), "http://first.invalid:7001")
        self.assertEqual(resolve_config_proxy(cfg, rotate=True), "socks5://second.invalid:7002")


if __name__ == "__main__":
    unittest.main()
