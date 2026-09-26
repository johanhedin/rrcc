"""Tests for the use of http_proxy, https_proxy and no_proxy."""

import os
import socket
import unittest

from support import PKI, TestCase
from support.repo import standard_repo
from support.servers import ProxyServer, StaticServer, TLSServer


class ProxyTest(TestCase):
    def setUp(self):
        super().setUp()
        standard_repo(self.tmp)

    def start(self, server):
        server.start()
        self.addCleanup(server.stop)
        return server

    def test_http_through_proxy(self):
        server = self.start(StaticServer(self.tmp))
        proxy = self.start(ProxyServer())
        result = self.rrcc("-j", "2", "--checksum", server.url,
                           env={"http_proxy": f"http://{proxy.address}"})
        self.assertConsistent(result)
        # every request went through the proxy, over kept-alive connections
        self.assertEqual(proxy.stats.get("GET", 0) + proxy.stats.get("HEAD", 0),
                         server.stats.get("GET", 0) + server.stats.get("HEAD", 0))
        self.assertGreater(proxy.stats["GET"], 6)
        self.assertLessEqual(proxy.stats["connections"], 3, proxy.stats)

    def test_upper_case_variable_and_no_scheme(self):
        server = self.start(StaticServer(self.tmp))
        proxy = self.start(ProxyServer())
        self.assertConsistent(self.rrcc(server.url, env={"HTTP_PROXY": proxy.address}))
        self.assertGreater(proxy.stats.get("GET", 0), 0)

    def test_https_through_connect(self):
        server = self.start(TLSServer(self.tmp, PKI))
        proxy = self.start(ProxyServer())
        result = self.rrcc("-j", "2", "--ca-cert", os.path.join(PKI, "ca.pem"), server.url,
                           env={"https_proxy": f"http://{proxy.address}"})
        self.assertConsistent(result)
        self.assertGreaterEqual(proxy.stats["CONNECT"], 1)
        self.assertLessEqual(proxy.stats["CONNECT"], 3, proxy.stats)  # one per thread
        self.assertEqual(proxy.stats.get("GET", 0), 0)

    def test_no_proxy(self):
        server = self.start(StaticServer(self.tmp))
        proxy = self.start(ProxyServer())
        result = self.rrcc(server.url, env={"http_proxy": f"http://{proxy.address}",
                                            "no_proxy": "example.com,127.0.0.1"})
        self.assertConsistent(result)
        self.assertEqual(proxy.stats.get("connections", 0), 0)

    def test_https_proxy_not_used_for_http(self):
        server = self.start(StaticServer(self.tmp))
        proxy = self.start(ProxyServer())
        self.assertConsistent(self.rrcc(server.url, env={"https_proxy": proxy.address}))
        self.assertEqual(proxy.stats.get("connections", 0), 0)

    def test_authentication(self):
        server = self.start(StaticServer(self.tmp))
        proxy = self.start(ProxyServer(auth="user:p@ss"))
        result = self.rrcc(server.url, env={"http_proxy": f"http://{proxy.address}"}, rc=2)
        self.assertIn("407", result.out)
        self.assertConsistent(self.rrcc(server.url, env={"http_proxy": f"http://user:p%40ss@{proxy.address}"}))

    def test_authentication_for_connect(self):
        server = self.start(TLSServer(self.tmp, PKI))
        proxy = self.start(ProxyServer(auth="user:p@ss"))
        ca = os.path.join(PKI, "ca.pem")
        result = self.rrcc("--ca-cert", ca, server.url,
                           env={"https_proxy": f"http://{proxy.address}"}, rc=2)
        self.assertIn("Tunnel connection failed: 407", result.out)
        self.assertConsistent(self.rrcc("--ca-cert", ca, server.url,
                                        env={"https_proxy": f"http://user:p%40ss@{proxy.address}"}))

    def test_dead_proxy(self):
        server = self.start(StaticServer(self.tmp))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        result = self.rrcc(server.url, env={"http_proxy": f"http://127.0.0.1:{port}"}, rc=2)
        self.assertIn("ConnectionRefusedError", result.out)

    def test_https_proxy_scheme_rejected(self):
        server = self.start(StaticServer(self.tmp))
        result = self.rrcc(server.url, env={"http_proxy": "https://127.0.0.1:3128"}, rc=2)
        self.assertIn("only http:// proxies are supported", result.out)


if __name__ == "__main__":
    unittest.main()
