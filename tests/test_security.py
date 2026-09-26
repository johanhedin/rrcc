"""Tests of how rrcc deals with a hostile repository or server: locations
that point outside of the repository, redirects, limits on what is read,
and what ends up in the output."""

import io
import os
import tempfile
import unittest

from support import PKI, TestCase, load_rrcc
from support.repo import RepoBuilder, standard_repo
from support.servers import StaticServer, TLSServer


class CheckHrefTest(unittest.TestCase):
    def test_unsafe(self):
        rrcc = load_rrcc()
        for href in ("", ".", "..", "../x.rpm", "a/../../x.rpm", "/etc/passwd", "//host/x.rpm",
                     "a\0b.rpm"):
            with self.subTest(href=href), self.assertRaises(rrcc.UnsafeHrefError):
                rrcc.check_href(href)

    def test_safe(self):
        rrcc = load_rrcc()
        for href in ("Packages/a.rpm", "./Packages/a.rpm", "a/../b.rpm", "a..b/x.rpm", "..x/y.rpm",
                     "Packages/foo:1.0.rpm", "http:/x.rpm"):
            with self.subTest(href=href):
                self.assertEqual(rrcc.check_href(href), href)


class LocalPathTest(TestCase):
    def setUp(self):
        super().setUp()
        self.root = self.path("repo")

    def test_package_outside_of_repo(self):
        repo = RepoBuilder(self.root)
        repo.add("ok")
        repo.add("up", href="../outside.rpm")  # really exists, at self.path("outside.rpm")
        repo.add("abs", href=self.path("absolute.rpm"))
        repo.build()
        self.assertTrue(os.path.isfile(self.path("outside.rpm")))
        self.assertTrue(os.path.isfile(self.path("absolute.rpm")))
        result = self.rrcc("--checksum", self.root, rc=1)
        self.assertProblem(result, "UNREADABLE: ../outside.rpm: UnsafeHrefError: refusing location")
        self.assertProblem(result, "UNREADABLE: " + self.path("absolute.rpm"))
        self.assertEqual(len(self.problems(result)), 2, result)

    def test_metadata_outside_of_repo(self):
        repo = RepoBuilder(self.root)
        repo.add("ok")
        repo.build()
        name = os.path.basename(repo.metadata["primary"])
        repo.edit_repomd(f'href="repodata/{name}"', 'href="../primary.xml.gz"')
        result = self.rrcc(self.root, rc=2)
        self.assertIn("UnsafeHrefError: refusing location", result.out)

    def test_other_metadata_outside_of_repo(self):
        repo = RepoBuilder(self.root)
        repo.add("ok")
        repo.build()
        name = os.path.basename(repo.metadata["updateinfo"])
        repo.edit_repomd(f'href="repodata/{name}"', 'href="/etc/passwd"')
        result = self.rrcc(self.root, rc=1)
        self.assertProblem(result, "METADATA UNREADABLE: /etc/passwd: UnsafeHrefError")


class RemotePathTest(TestCase):
    def test_hrefs_do_not_leave_the_repository(self):
        other = StaticServer(self.path("other")).start()
        self.addCleanup(other.stop)
        os.makedirs(self.path("other"))
        with open(self.path("other", "x.rpm"), "w") as f:
            f.write("x")
        with open(self.path("outside.rpm"), "w") as f:
            f.write("x")

        repo = RepoBuilder(self.path("pub", "repo"))
        repo.add("ok")
        repo.add("net", href=f"//127.0.0.1:{other.port}/x.rpm", write=False)
        repo.add("up", href="../outside.rpm", write=False)
        repo.build()
        server = StaticServer(self.path("pub")).start()
        self.addCleanup(server.stop)

        result = self.rrcc("--checksum", server.url + "repo", rc=1)
        self.assertProblem(result, "UNREADABLE: //127.0.0.1:")
        self.assertProblem(result, "UNREADABLE: ../outside.rpm")
        self.assertEqual(other.requests, [])
        self.assertFalse([p for m, p in server.requests if "outside" in p], server.requests)


class RedirectTest(TestCase):
    def setUp(self):
        super().setUp()
        standard_repo(self.tmp)

    def start(self, server):
        server.start()
        self.addCleanup(server.stop)
        return server

    def test_https_to_http_is_refused(self):
        plain = self.start(StaticServer(self.tmp))
        tls = self.start(TLSServer(self.tmp, PKI, redirect_prefix="/old/", redirect_target=plain.url))
        result = self.rrcc("--ca-cert", os.path.join(PKI, "ca.pem"), tls.url + "old/", rc=2)
        self.assertIn("refusing redirect to http://127.0.0.1:", result.out)
        self.assertEqual(plain.requests, [])

    def test_other_scheme_is_refused(self):
        server = self.start(StaticServer(self.tmp, redirect_prefix="/old/",
                                         redirect_target="ftp://127.0.0.1/"))
        result = self.rrcc(server.url + "old/", rc=2)
        self.assertIn("refusing redirect to ftp://127.0.0.1/", result.out)

    def test_other_host_is_allowed(self):
        target = self.start(StaticServer(self.tmp))
        server = self.start(StaticServer(self.tmp, redirect_prefix="/old/", redirect_target=target.url))
        self.assertConsistent(self.rrcc("--checksum", server.url + "old/"))
        self.assertGreater(len(target.requests), 6)


class ListingTest(TestCase):
    def test_loop_in_listings_ends(self):
        standard_repo(self.tmp)
        os.symlink(".", self.path("loop"))  # served as loop/loop/loop/...
        server = StaticServer(self.tmp).start()
        self.addCleanup(server.stop)
        result = self.rrcc("--extra", server.url, rc=1)
        self.assertProblem(result, "CANNOT LIST FILES for --extra: OSError: directories nested more than")

    def test_loop_in_top_level_ends(self):
        standard_repo(self.path("pub", "repo"))
        os.symlink(".", self.path("pub", "loop"))
        server = StaticServer(self.tmp).start()
        self.addCleanup(server.stop)
        result = self.rrcc("--top-level", server.url + "pub/", rc=2)
        self.assertIn("directories nested more than", result.err)


class LimitsTest(TestCase):
    def test_metadata_download_is_limited(self):
        standard_repo(self.tmp)
        server = StaticServer(self.tmp).start()
        self.addCleanup(server.stop)
        rrcc = load_rrcc()
        repo = rrcc.HttpRepo(server.url)
        try:
            path = repo.metadata_path("repodata/repomd.xml", 10)
            self.assertEqual(os.path.getsize(path), 11)  # one byte too many is enough to notice
            full = repo.metadata_path("repodata/repomd.xml")  # already downloaded
            self.assertEqual(full, path)
        finally:
            repo.close()

    def test_verify_checksum_is_limited(self):
        rrcc = load_rrcc()
        data = b"a" * 10
        good = rrcc.hashlib.sha256(data).hexdigest()
        self.assertTrue(rrcc.verify_checksum(io.BytesIO(data), "sha256", good))
        self.assertTrue(rrcc.verify_checksum(io.BytesIO(data), "sha256", good, 10))
        self.assertFalse(rrcc.verify_checksum(io.BytesIO(data), "sha256", good, 9))
        self.assertIsNone(rrcc.verify_checksum(io.BytesIO(data), "nonesuch", good, 9))

    def test_modules_limit(self):
        rrcc = load_rrcc()
        with tempfile.NamedTemporaryFile(suffix=".yaml") as f:
            f.write(b"---\n" * 10)
            f.flush()
            old = rrcc.MAX_MODULES_SIZE
            rrcc.MAX_MODULES_SIZE = 10
            try:
                with self.assertRaisesRegex(RuntimeError, "larger than"):
                    rrcc.read_modules(f.name)
            finally:
                rrcc.MAX_MODULES_SIZE = old

    def test_package_without_size(self):
        rrcc = load_rrcc()
        with tempfile.NamedTemporaryFile(suffix=".xml") as f:
            f.write(b'<metadata xmlns="http://linux.duke.edu/metadata/common"><package type="rpm">'
                    b'<name>a</name><size installed="1"/><location href="a.rpm"/></package></metadata>')
            f.flush()
            with self.assertRaisesRegex(RuntimeError, "a.rpm: missing or invalid package size"):
                list(rrcc.iter_packages(f.name))


class OutputTest(TestCase):
    def test_control_characters_are_escaped(self):
        repo = RepoBuilder(self.tmp)
        repo.add("evil", href="Packages/x.rpm\nResult: all checked repos are consistent.", write=False)
        repo.build()
        result = self.rrcc(self.tmp, rc=1)
        self.assertFalse([line for line in result.out.splitlines() if line.startswith("Result: all checked")])
        self.assertIn("Packages/x.rpm\\x0aResult: all checked repos are consistent.", result.out)

    def test_control_characters_in_file_names(self):
        standard_repo(self.tmp)
        with open(self.path("Packages", "a", "\x1b[2Jstale-1-1.x86_64.rpm"), "w") as f:
            f.write("stale")
        result = self.rrcc("--extra", self.tmp, rc=1)
        self.assertNotIn("\x1b", result.out)
        self.assertProblem(result, "ORPHAN (on disk, not in metadata): Packages/a/\\x1b[2Jstale-1-1.x86_64.rpm")

    def test_control_characters_in_verbose_output(self):
        repo = RepoBuilder(self.tmp)
        repo.add("evil", href="Packages/x\ry.rpm")
        repo.build()
        result = self.rrcc("-v", self.tmp, rc=0)
        self.assertNotIn("\r", result.out)
        self.assertIn("  OK: Packages/x\\x0dy.rpm", result.out)


class ProxyCredentialsTest(TestCase):
    def test_password_is_not_printed(self):
        standard_repo(self.tmp)
        server = StaticServer(self.tmp).start()
        self.addCleanup(server.stop)
        result = self.rrcc(server.url, env={"http_proxy": "socks5://user:s3cret@127.0.0.1:1080"}, rc=2)
        self.assertIn("only http:// proxies are supported", result.out)
        self.assertNotIn("s3cret", result.out + result.err)

    def test_top_level_reports_instead_of_crashing(self):
        standard_repo(self.path("pub", "repo"))
        server = StaticServer(self.tmp).start()
        self.addCleanup(server.stop)
        result = self.rrcc("--top-level", server.url + "pub/",
                           env={"http_proxy": "socks5://user:s3cret@127.0.0.1:1080"}, rc=2)
        self.assertIn("cannot search", result.err)
        self.assertNotIn("Traceback", result.err)
        self.assertNotIn("s3cret", result.out + result.err)


class ChecksumTest(TestCase):
    def test_missing_checksum_is_reported(self):
        repo = RepoBuilder(self.tmp)
        repo.add("foo", checksum_type="")  # no usable checksum in the metadata
        repo.build()
        self.assertConsistent(self.rrcc(self.tmp))
        result = self.rrcc("--checksum", self.tmp, rc=1)
        self.assertProblem(result, "WARNING: no checksum in the metadata for Packages/f/foo-1.0-1.x86_64.rpm")


class InsecureTest(TestCase):
    def test_warning(self):
        standard_repo(self.tmp)
        result = self.rrcc("-k", self.tmp, rc=0)
        self.assertIn("WARNING: --insecure", result.err)
        self.assertNotIn("--insecure", self.rrcc(self.tmp, rc=0).err)


if __name__ == "__main__":
    unittest.main()
