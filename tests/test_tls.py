"""Tests for https:// repositories and the TLS options."""

import os
import unittest

from support import PKI, TestCase
from support.repo import standard_repo
from support.servers import StaticServer, TLSServer


def pki(name):
    return os.path.join(PKI, name)


class TlsTest(TestCase):
    def setUp(self):
        super().setUp()
        standard_repo(self.tmp)

    def serve(self, **kwargs):
        server = TLSServer(self.tmp, PKI, **kwargs).start()
        self.addCleanup(server.stop)
        return server

    def test_untrusted_by_default(self):
        result = self.rrcc(self.serve().url, rc=2)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", result.out)

    def test_ca_cert_file(self):
        self.assertConsistent(self.rrcc("--ca-cert", pki("ca.pem"), "--checksum", self.serve().url))

    def test_ca_cert_directory(self):
        self.assertConsistent(self.rrcc("--ca-cert", pki("cadir"), self.serve().url))

    def test_ssl_cert_file_environment(self):
        self.assertConsistent(self.rrcc(self.serve().url, env={"SSL_CERT_FILE": pki("ca.pem")}))

    def test_ca_cert_replaces_system_cas(self):
        # --ca-cert must not add to what SSL_CERT_FILE (the "system") trusts
        result = self.rrcc("--ca-cert", pki("other-ca.pem"), self.serve().url,
                           env={"SSL_CERT_FILE": pki("ca.pem")}, rc=2)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", result.out)

    def test_wrong_host_name(self):
        server = self.serve()
        url = f"https://127.0.0.1:{server.port}/"  # the certificate is for localhost
        result = self.rrcc("--ca-cert", pki("ca.pem"), url, rc=2)
        self.assertIn("127.0.0.1", result.out)
        self.assertConsistent(self.rrcc("-k", url))

    def test_insecure(self):
        self.assertConsistent(self.rrcc("--insecure", self.serve().url))

    def test_tls_options_ignored_for_http(self):
        server = StaticServer(self.tmp).start()
        self.addCleanup(server.stop)
        self.assertConsistent(self.rrcc("-k", "--ca-cert", pki("ca.pem"), server.url))

    def test_top_level(self):
        server = self.serve()
        result = self.rrcc("--top-level", "--ca-cert", pki("ca.pem"), server.url, rc=0)
        self.assertIn("Found 1 repo(s) under", result.out)


class ClientCertTest(TestCase):
    def setUp(self):
        super().setUp()
        standard_repo(self.tmp)
        server = TLSServer(self.tmp, PKI, require_client_cert=True).start()
        self.addCleanup(server.stop)
        self.url = server.url

    def test_required(self):
        result = self.rrcc("--ca-cert", pki("ca.pem"), self.url, rc=2)
        # With TLS 1.3 the server rejects the client after the handshake, so
        # the client sees either the TLS alert or a reset connection.
        self.assertRegex(result.out, r"ERROR: (SSLError|ConnectionResetError)")

    def test_separate_key(self):
        self.assertConsistent(self.rrcc("--ca-cert", pki("ca.pem"), "--client-cert", pki("client.pem"),
                                        "--client-key", pki("client.key"), "-j", "3", "--checksum",
                                        self.url))

    def test_combined_file(self):
        self.assertConsistent(self.rrcc("--ca-cert", pki("ca.pem"),
                                        "--client-cert", pki("client-combined.pem"), self.url))


class TlsArgumentTest(TestCase):
    """Unusable TLS options are command line errors (exit status 2)."""

    def check(self, *args, message):
        result = self.rrcc(*args, "https://localhost:1/", rc=2)
        self.assertIn(message, result.err)
        self.assertEqual(result.out, "")

    def test_key_without_cert(self):
        self.check("--client-key", pki("client.key"), message="--client-key needs --client-cert")

    def test_missing_ca(self):
        self.check("--ca-cert", self.path("nope.pem"),
                   message=f"--ca-cert {self.path('nope.pem')}: no such file or directory")

    def test_missing_key(self):
        self.check("--client-cert", pki("client.pem"), "--client-key", self.path("nope.key"),
                   message="--client-key " + self.path("nope.key") + ": no such file or directory")

    def test_ca_is_not_a_certificate(self):
        self.check("--ca-cert", pki("client.key"), message="--ca-cert " + pki("client.key") + ":")

    def test_cert_without_key(self):
        self.check("--client-cert", pki("client.pem"),
                   message="cannot read a certificate and private key from the file")

    def test_key_does_not_match(self):
        self.check("--client-cert", pki("client.pem"), "--client-key", pki("server.key"),
                   message="does not belong to the certificate in")

    def test_encrypted_key(self):
        self.check("--client-cert", pki("client.pem"), "--client-key", pki("client-encrypted.key"),
                   message="encrypted private keys are not supported")


if __name__ == "__main__":
    unittest.main()
