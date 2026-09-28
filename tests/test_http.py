"""End-to-end tests of rrcc on repositories served over HTTP, by a server
in the test process."""

import os
import socket
import unittest

from support import TestCase
from support.repo import RepoBuilder, flip_byte, standard_repo, truncate
from support.servers import StaticServer


class HttpTestCase(TestCase):
    def serve(self, **kwargs):
        """Start a StaticServer for self.tmp, stopped at the end of the test."""
        server = StaticServer(self.tmp, **kwargs).start()
        self.addCleanup(server.stop)
        return server


class HttpConsistencyTest(HttpTestCase):
    def test_consistent(self):
        standard_repo(self.tmp)
        server = self.serve()
        result = self.rrcc(server.url)
        self.assertConsistent(result)
        self.assertIn("6 packages checked, consistent (size-checked).", result.out)
        # packages are only HEAD-checked without --checksum
        package_gets = [p for m, p in server.requests if m == "GET" and p.startswith("/Packages/")]
        self.assertEqual(package_gets, [])

    def test_consistent_with_checksum(self):
        standard_repo(self.tmp)
        result = self.rrcc("--checksum", self.serve().url)
        self.assertConsistent(result)
        self.assertIn("consistent (checksums verified).", result.out)

    def test_url_without_trailing_slash(self):
        standard_repo(self.path("repo"))
        self.assertConsistent(self.rrcc(self.serve().url + "repo"))

    def test_missing_package(self):
        repo = standard_repo(self.tmp)
        os.remove(repo.package_path("beta"))
        result = self.rrcc(self.serve().url, rc=1)
        # the + in the name must be URL-quoted correctly
        self.assertProblem(result, "MISSING: Packages/b/beta-2.0-3.module_el8+1234+abcd.x86_64.rpm")
        self.assertEqual(len(self.problems(result)), 1)

    def test_size_mismatch(self):
        repo = standard_repo(self.tmp)
        truncate(repo.package_path("gamma"), 3)
        result = self.rrcc(self.serve().url, rc=1)
        self.assertProblem(result, "SIZE MISMATCH: Packages/g/gamma-0.9^git20260101-1.x86_64.rpm")

    def test_checksum_mismatch(self):
        repo = standard_repo(self.tmp)
        flip_byte(repo.package_path("delta"))
        server = self.serve()
        self.assertConsistent(self.rrcc(server.url))
        self.assertProblem(self.rrcc("--checksum", server.url, rc=1),
                           "CHECKSUM MISMATCH: Packages/d/delta-5-1.x86_64.rpm")

    def test_newest_only_with_modules(self):
        repo = RepoBuilder(self.tmp)
        repo.add("a", "1", "1.module+1", write=False)
        repo.add("a", "2", "1.module+2")
        repo.add_module("m", "s", 1, ["a-0:1-1.module+1.x86_64"])
        repo.add_module("m", "s", 2, ["a-0:2-1.module+2.x86_64"])
        repo.build()
        server = self.serve()
        self.rrcc(server.url, rc=1)
        result = self.rrcc("-n", server.url, rc=0)
        self.assertIn("skipped 1 older package(s)", result.out)


class HttpMetadataTest(HttpTestCase):
    def setUp(self):
        super().setUp()
        self.repo = standard_repo(self.tmp)
        self.server = self.serve()

    def test_size_only_by_default(self):
        result = self.rrcc(self.server.url, rc=0)
        self.assertIn("4 metadata file(s) in repomd.xml checked (1 with checksum, 3 size only; "
                      "--checksum verifies all)", result.out)
        metadata_gets = {p for m, p in self.server.requests if m == "GET" and p.startswith("/repodata/")}
        self.assertEqual(len(metadata_gets), 2, metadata_gets)  # repomd.xml and primary

    def test_all_with_checksum(self):
        result = self.rrcc("--checksum", self.server.url, rc=0)
        self.assertIn("4 metadata file(s) in repomd.xml checked (size and checksum)", result.out)

    def test_truncated_found_by_default(self):
        truncate(self.repo.metadata["filelists"], 100)
        self.assertProblem(self.rrcc(self.server.url, rc=1), "METADATA SIZE MISMATCH")

    def test_corrupt_found_with_checksum(self):
        flip_byte(self.repo.metadata["other"])
        self.assertConsistent(self.rrcc(self.server.url))
        self.assertProblem(self.rrcc("--checksum", self.server.url, rc=1),
                           "METADATA CHECKSUM MISMATCH")

    def test_missing(self):
        os.remove(self.repo.metadata["updateinfo"])
        self.assertProblem(self.rrcc(self.server.url, rc=1), "METADATA MISSING")

    def test_corrupt_primary_is_an_error(self):
        flip_byte(self.repo.metadata["primary"])
        result = self.rrcc(self.server.url, rc=2)
        self.assertIn("primary metadata file does not match repomd.xml", result.out)


class ConnectionTest(HttpTestCase):
    def setUp(self):
        super().setUp()
        repo = RepoBuilder(self.tmp)
        for i in range(40):
            repo.add(f"pkg{i:02d}")
        repo.build()

    def test_keep_alive_single_connection(self):
        server = self.serve()
        self.assertConsistent(self.rrcc("-j", "1", "--checksum", server.url))
        self.assertEqual(server.stats["connections"], 1, server.stats)
        self.assertGreaterEqual(server.stats["GET"], 40)

    def test_one_connection_per_worker(self):
        server = self.serve()
        self.assertConsistent(self.rrcc("-j", "4", server.url))
        # the main thread (metadata) plus at most one per worker
        self.assertLessEqual(server.stats["connections"], 5, server.stats)
        self.assertGreaterEqual(server.stats["HEAD"], 40)

    def test_server_drops_idle_connections(self):
        server = self.serve(drop_after_request=True)
        result = self.rrcc("-j", "4", "--checksum", server.url)
        self.assertConsistent(result)
        self.assertNotIn("UNREADABLE", result.out)

    def test_redirect(self):
        server = self.serve(redirect_prefix="/old/")
        self.assertConsistent(self.rrcc("--checksum", server.url + "old/"))

    def test_not_found(self):
        result = self.rrcc(self.serve().url + "nothing/here/", rc=2)
        self.assertIn("ERROR: missing http://127.0.0.1:", result.out)

    def test_connection_refused(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # nothing listens on port now
        result = self.rrcc(f"http://127.0.0.1:{port}/", rc=2)
        self.assertIn("ConnectionRefusedError", result.out)


class HttpListingTest(HttpTestCase):
    def test_orphans(self):
        standard_repo(self.tmp)
        with open(self.path("Packages", "a", "stale-1-1.x86_64.rpm"), "w") as f:
            f.write("stale")
        result = self.rrcc("--extra", self.serve().url, rc=1)
        self.assertEqual(self.problems(result),
                         ["ORPHAN (on disk, not in metadata): Packages/a/stale-1-1.x86_64.rpm"])

    def test_no_orphans(self):
        standard_repo(self.tmp)
        self.assertConsistent(self.rrcc("--extra", self.serve().url))

    def test_orphans_need_listings(self):
        standard_repo(self.tmp)
        result = self.rrcc("--extra", self.serve(listings=False).url, rc=1)
        self.assertProblem(result, "CANNOT LIST FILES for --extra")
        self.assertIn("6 packages checked", result.out)

    def test_top_level(self):
        standard_repo(self.path("pub", "os", "x86_64"))
        standard_repo(self.path("pub", "updates", "x86_64"))
        os.makedirs(self.path("pub", "empty"))
        server = self.serve()
        result = self.rrcc("--top-level", server.url + "pub/", rc=0)
        self.assertIn("Found 2 repo(s) under", result.out)
        self.assertIn("=== os/x86_64/ ===", result.out)
        self.assertIn("Summary: 2 repo(s), 12 package(s) checked total.", result.out)

    def write_page(self, html, *parts):
        os.makedirs(self.path(*parts[:-1]), exist_ok=True)
        with open(self.path(*parts), "w") as f:
            f.write(html)

    def test_top_level_index_html_listing(self):
        # Like the static indexes generated for an S3 bucket: directories
        # are linked as dir/index.html, and the parent as ../index.html.
        standard_repo(self.path("pub", "os", "x86_64"))
        self.write_page('<a href="index_by_size.html">Size</a> <a href="../index.html">Parent</a>'
                        '<a href="os/index.html">os/</a> <a href="README.txt">README.txt</a>',
                        "pub", "index.html")
        self.write_page('<a href="../index.html">Parent Directory</a>'
                        '<a href="x86_64/index.html">x86_64/</a>', "pub", "os", "index.html")
        result = self.rrcc("--top-level", self.serve().url + "pub/", rc=0)
        self.assertIn("Found 1 repo(s) under", result.out)
        self.assertIn("=== os/x86_64/ ===", result.out)

    def test_top_level_links_without_slash(self):
        # Like a hand-made mirror page: directories linked without the
        # trailing slash, which the server adds with a redirect.
        standard_repo(self.path("pub", "os"))
        standard_repo(self.path("pub", "text-hint"))
        self.write_page("key", "pub", "RPM-GPG-KEY-test")
        self.write_page('<a href="/pub/os">Base OS</a> <a href="text-hint">text-hint/</a>'
                        '<a href="RPM-GPG-KEY-test">GPG key</a> <a href="gone">Gone</a>',
                        "pub", "index.html")
        server = self.serve()
        result = self.rrcc("--top-level", server.url + "pub/", rc=0)
        self.assertIn("Found 2 repo(s) under", result.out)
        self.assertIn("=== os/ ===", result.out)
        self.assertIn("=== text-hint/ ===", result.out)
        self.assertNotIn(("HEAD", "/pub/text-hint"), server.requests)  # the link text was enough

    def test_top_level_nothing_found(self):
        os.makedirs(self.path("pub", "empty"))
        result = self.rrcc("--top-level", self.serve().url + "pub/", rc=2)
        self.assertIn("no repos found under", result.err)

    def test_top_level_without_listings(self):
        standard_repo(self.path("pub", "os"))
        result = self.rrcc("--top-level", self.serve(listings=False).url + "pub/", rc=2)
        self.assertIn("cannot search", result.err)


if __name__ == "__main__":
    unittest.main()
