"""Tests of how rrcc deals with a hostile repository or server: locations
that point outside of the repository, redirects, limits on what is read,
and what ends up in the output."""

import io
import os
import tempfile
import unittest

from support import PKI, TestCase, load_rrcc
from support.repo import RepoBuilder, standard_repo
from support.servers import GarbageServer, StaticServer, TLSServer


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


class SymlinkTest(TestCase):
    def test_package_symlink_outside_repo_is_refused(self):
        repo = RepoBuilder(self.path("repo"))
        repo.add("ok")
        pkg = repo.add("evil", write=False)
        repo.build()
        with open(self.path("secret"), "wb") as f:
            f.write(pkg["content"])  # matches what the metadata expects
        link = self.path("repo", pkg["href"])
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(self.path("secret"), link)

        result = self.rrcc("--checksum", self.path("repo"), rc=1)
        self.assertProblem(result, f"UNREADABLE: {pkg['href']}: UnsafeHrefError: "
                                   "refusing location that resolves outside of the repository")
        # the symlink is refused before it is ever opened, so nothing about
        # the target (its size, or that its checksum matches) is reported
        self.assertNotIn(str(len(pkg["content"])), result.out)

        # --allow-symlinks-outside opts back in, for a mirror that symlinks
        # packages in from a shared pool on purpose
        self.assertConsistent(self.rrcc("--allow-symlinks-outside", "--checksum", self.path("repo")))

    def test_symlink_inside_repo_is_fine(self):
        repo = RepoBuilder(self.path("repo"))
        pkg = repo.add("ok")
        repo.build()
        real = self.path("repo", "real-ok.rpm")
        os.rename(self.path("repo", pkg["href"]), real)
        os.symlink(real, self.path("repo", pkg["href"]))
        self.assertConsistent(self.rrcc("--checksum", self.path("repo")))

    def test_top_level_symlinked_repo_dir(self):
        standard_repo(self.path("real", "repo"))
        os.makedirs(self.path("pub"))
        os.symlink(self.path("real"), self.path("pub", "link"))

        result = self.rrcc("--top-level", self.path("pub"), rc=2)
        self.assertIn("no repos found under", result.err)

        result = self.rrcc("--top-level", "--allow-symlinks-outside", self.path("pub"), rc=0)
        self.assertIn("Found 1 repo(s) under", result.out)


class DoctypeTest(TestCase):
    def test_repomd_doctype_is_refused(self):
        repo = RepoBuilder(self.tmp)
        repo.add("ok")
        repo.build()
        repo.edit_repomd('<repomd xmlns="http://linux.duke.edu/metadata/repo"',
                         '<!DOCTYPE repomd [<!ENTITY x "y">]>\n'
                         '<repomd xmlns="http://linux.duke.edu/metadata/repo"')
        result = self.rrcc(self.tmp, rc=2)
        self.assertIn("DOCTYPE", result.out)

    def test_primary_doctype_is_refused(self):
        rrcc = load_rrcc()
        xml = (b'<?xml version="1.0"?>\n'
               b'<!DOCTYPE metadata [<!ENTITY x "y">]>\n'
               b'<metadata xmlns="http://linux.duke.edu/metadata/common" packages="0">'
               b'</metadata>\n')
        with tempfile.NamedTemporaryFile(suffix=".xml") as f:
            f.write(xml)
            f.flush()
            with self.assertRaisesRegex(ValueError, "DOCTYPE"):
                list(rrcc.iter_packages(f.name))


class ListingLimitsTest(TestCase):
    """These call the internal functions directly (rather than running rrcc
    as a subprocess, like self.rrcc() does), so that the size/count limits
    can be lowered just for the test."""

    def test_listing_larger_than_limit_is_refused(self):
        standard_repo(self.tmp)
        server = StaticServer(self.tmp).start()
        self.addCleanup(server.stop)
        rrcc = load_rrcc()
        client = rrcc.HttpClient()
        self.addCleanup(client.close)
        old = rrcc.MAX_LISTING_SIZE
        rrcc.MAX_LISTING_SIZE = 10
        try:
            with self.assertRaisesRegex(OSError, "larger than 10 bytes"):
                rrcc.list_http_dir(client, server.url)
        finally:
            rrcc.MAX_LISTING_SIZE = old

    def test_links_probed_for_directories_are_limited(self):
        os.makedirs(self.path("pub"))
        links = [f'<a href="k{i}">k{i}</a>' for i in range(10)]
        links += ['<a href="a.rpm">a.rpm</a>', '<a href="repomd.xml">repomd.xml</a>']
        with open(self.path("pub", "index.html"), "w") as f:
            f.write("".join(links))
        server = StaticServer(self.tmp).start()
        self.addCleanup(server.stop)
        rrcc = load_rrcc()
        client = rrcc.HttpClient()
        self.addCleanup(client.close)
        old = rrcc.MAX_LISTING_PROBES
        rrcc.MAX_LISTING_PROBES = 3
        try:
            dirs, files = rrcc.list_http_dir(client, server.url + "pub/")
        finally:
            rrcc.MAX_LISTING_PROBES = old
        self.assertEqual(dirs, [])
        self.assertEqual(len(files), 12)
        self.assertEqual([p for m, p in server.requests if m == "HEAD"], ["/pub/k0", "/pub/k1", "/pub/k2"])

    def test_too_many_directories(self):
        standard_repo(self.path("pub", "repo"))
        for i in range(5):
            os.makedirs(self.path("pub", f"d{i}"))
        server = StaticServer(self.tmp).start()
        self.addCleanup(server.stop)
        rrcc = load_rrcc()
        old = rrcc.MAX_LISTED_DIRS
        rrcc.MAX_LISTED_DIRS = 3
        try:
            with self.assertRaisesRegex(OSError, "more than 3 directories"):
                rrcc.find_http_repos(server.url + "pub/")
        finally:
            rrcc.MAX_LISTED_DIRS = old


class MalformedResponseTest(TestCase):
    def test_bad_status_line_does_not_crash(self):
        server = GarbageServer().start()
        self.addCleanup(server.stop)
        result = self.rrcc(server.url, rc=2)
        self.assertNotIn("Traceback", result.err)
        self.assertIn("ERROR: ", result.out)
        self.assertIn("BadStatusLine", result.out)

    def test_top_level_reports_instead_of_crashing(self):
        server = GarbageServer().start()
        self.addCleanup(server.stop)
        result = self.rrcc("--top-level", server.url, rc=2)
        self.assertNotIn("Traceback", result.err)
        self.assertIn("cannot search", result.err)
        self.assertIn("BadStatusLine", result.err)


class ClientCertRedirectTest(TestCase):
    """A --client-cert must only be sent to the host the user named, not to
    a host reached through a redirect (see HttpClient._context_for)."""

    def rrcc_with_cert(self, *args, **kwargs):
        return self.rrcc("--ca-cert", os.path.join(PKI, "ca.pem"),
                         "--client-cert", os.path.join(PKI, "client.pem"),
                         "--client-key", os.path.join(PKI, "client.key"), *args, **kwargs)

    def test_client_cert_not_sent_to_redirect_target(self):
        standard_repo(self.tmp)
        target = TLSServer(self.tmp, PKI, require_client_cert=True).start()
        self.addCleanup(target.stop)
        front = TLSServer(self.tmp, PKI, redirect_prefix="/old/", redirect_target=target.url).start()
        self.addCleanup(front.stop)

        result = self.rrcc_with_cert(front.url + "old/", rc=2)
        # Like ClientCertTest.test_required in test_tls.py: with no client
        # certificate presented, the target's TLS 1.3 handshake fails after
        # the fact, seen as either the alert or a reset connection.
        self.assertRegex(result.out, r"ERROR: (SSLError|ConnectionResetError)")

    def test_client_cert_is_sent_to_the_same_host(self):
        # control case: the redirect target does require the certificate,
        # but is the very host rrcc was pointed at (different path, not
        # different host), so it must still be sent.
        standard_repo(self.path("repo"))
        server = TLSServer(self.path("repo"), PKI, require_client_cert=True,
                           redirect_prefix="/old/", redirect_target=None).start()
        self.addCleanup(server.stop)
        self.assertConsistent(self.rrcc_with_cert("--checksum", server.url + "old/"))


class BidiTest(TestCase):
    def test_bidi_override_is_escaped(self):
        bidi_override = chr(0x202e)  # RIGHT-TO-LEFT OVERRIDE
        repo = RepoBuilder(self.tmp)
        repo.add("evil", href=f"Packages/x{bidi_override}evil.rpm")
        repo.build()
        result = self.rrcc("-v", self.tmp, rc=0)
        self.assertNotIn(bidi_override, result.out)
        self.assertIn("Packages/x\\x202eevil.rpm", result.out)


if __name__ == "__main__":
    unittest.main()
