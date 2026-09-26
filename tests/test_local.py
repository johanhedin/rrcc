"""End-to-end tests of the rrcc command on repositories in directories."""

import os
import unittest

from support import TestCase, have_command, have_zstd, load_rrcc
from support.repo import RepoBuilder, flip_byte, standard_repo, truncate


class ConsistencyTest(TestCase):
    def test_consistent(self):
        standard_repo(self.tmp)
        result = self.rrcc(self.tmp)
        self.assertConsistent(result)
        self.assertIn("6 packages checked, consistent (size-checked).", result.out)
        self.assertEqual(self.problems(result), [])

    def test_consistent_with_checksum(self):
        standard_repo(self.tmp)
        result = self.rrcc("--checksum", self.tmp)
        self.assertConsistent(result)
        self.assertIn("consistent (checksums verified).", result.out)

    def test_missing_package(self):
        repo = standard_repo(self.tmp)
        os.remove(repo.package_path("beta"))
        result = self.rrcc(self.tmp, rc=1)
        self.assertProblem(result, "MISSING: Packages/b/beta-2.0-3.module_el8+1234+abcd.x86_64.rpm")
        self.assertIn("Result: inconsistencies found.", result.out)

    def test_size_mismatch(self):
        repo = standard_repo(self.tmp)
        truncate(repo.package_path("alpha", "1.0"), 5)
        result = self.rrcc(self.tmp, rc=1)
        self.assertProblem(result, "SIZE MISMATCH: Packages/a/alpha-1.0-1.x86_64.rpm")

    def test_checksum_mismatch_needs_checksum_option(self):
        repo = standard_repo(self.tmp)
        flip_byte(repo.package_path("gamma"))
        self.assertConsistent(self.rrcc(self.tmp))
        result = self.rrcc("--checksum", self.tmp, rc=1)
        self.assertProblem(result, "CHECKSUM MISMATCH: Packages/g/gamma-0.9^git20260101-1.x86_64.rpm")

    def test_unsupported_checksum_algorithm(self):
        repo = RepoBuilder(self.tmp)
        repo.add("foo", checksum_type="sha3_999")
        repo.build()
        self.assertConsistent(self.rrcc(self.tmp))
        result = self.rrcc("--checksum", self.tmp, rc=1)
        self.assertProblem(result, "WARNING: unsupported checksum algo 'sha3_999'")

    def test_sha1_and_sha512_packages(self):
        repo = RepoBuilder(self.tmp)
        repo.add("one", checksum_type="sha1")
        repo.add("two", checksum_type="sha512")
        repo.build()
        self.assertConsistent(self.rrcc("--checksum", self.tmp))

    def test_verbose(self):
        standard_repo(self.tmp)
        result = self.rrcc("-v", self.tmp, rc=0)
        self.assertIn("  OK: Packages/a/alpha-1.1-1.noarch.rpm", result.out)
        self.assertIn("  OK: repodata/", result.out)

    def test_orphans(self):
        repo = standard_repo(self.tmp)
        os.makedirs(self.path("Packages", "z"))
        with open(self.path("Packages", "z", "stale-1-1.x86_64.rpm"), "w") as f:
            f.write("stale")
        with open(self.path("Packages", "z", "notes.txt"), "w") as f:
            f.write("not an rpm")
        self.assertConsistent(self.rrcc(self.tmp))
        result = self.rrcc("--extra", self.tmp, rc=1)
        self.assertEqual([p for p in self.problems(result) if p.startswith("ORPHAN")],
                         ["ORPHAN (on disk, not in metadata): Packages/z/stale-1-1.x86_64.rpm"])

    def test_orphans_ignore_repodata(self):
        standard_repo(self.tmp)
        with open(self.path("repodata", "leftover.rpm"), "w") as f:
            f.write("x")
        self.assertConsistent(self.rrcc("--extra", self.tmp))


class CompressionTest(TestCase):
    def check(self, fmt):
        standard_repo(self.tmp, primary=fmt)
        self.assertConsistent(self.rrcc("--checksum", self.tmp))

    def test_gz(self):
        self.check("gz")

    def test_xz(self):
        self.check("xz")

    def test_uncompressed(self):
        self.check("none")

    @unittest.skipUnless(have_zstd(), "needs Python 3.14+ or the zstandard module")
    def test_zst(self):
        self.check("zst")

    @unittest.skipUnless(have_command("zck") and have_command("unzck"), "needs zck and unzck (zchunk)")
    def test_zck(self):
        self.check("zck")


@unittest.skipUnless(have_command("zck") and have_command("unzck"), "needs zck and unzck (zchunk)")
class PrimaryZckTest(TestCase):
    def test_same_as_primary(self):
        standard_repo(self.tmp, primary_zck=True)
        result = self.rrcc(self.tmp, rc=0)
        self.assertIn("primary_zck cross-checked against primary (6 packages)", result.out)

    def test_differs_from_primary(self):
        repo = RepoBuilder(self.tmp)
        a = repo.add("a")
        b = repo.add("b")
        extra = dict(repo.add("c", write=False))
        repo.packages.remove(extra)
        changed = dict(b, content=b["content"] + b"x")
        repo.build(primary_zck=True, zck_packages=[changed, extra])
        result = self.rrcc(self.tmp, rc=1)
        self.assertProblem(result, "PRIMARY_ZCK EXTRA: in primary_zck but not in primary: " + extra["href"])
        self.assertProblem(result, "PRIMARY_ZCK MISMATCH: size/checksum differs from primary: " + b["href"])
        self.assertProblem(result, "PRIMARY_ZCK MISSING PACKAGE: in primary but not in primary_zck: " + a["href"])

    def test_missing_zck_reported_once(self):
        repo = standard_repo(self.tmp, primary_zck=True)
        os.remove(repo.metadata["primary_zck"])
        result = self.rrcc(self.tmp, rc=1)
        self.assertProblem(result, "METADATA MISSING: repodata/")
        self.assertNoProblem(result, "PRIMARY_ZCK")


class NewestOnlyTest(TestCase):
    def test_older_versions_skipped(self):
        repo = standard_repo(self.tmp)
        os.remove(repo.package_path("alpha", "1.0"))
        self.rrcc(self.tmp, rc=1)
        result = self.rrcc("-n", self.tmp, rc=0)
        self.assertIn("5 packages checked, consistent", result.out)
        self.assertIn("--newest-only: skipped 1 older package(s), 6 in metadata", result.out)

    def test_latest_still_checked(self):
        repo = standard_repo(self.tmp)
        os.remove(repo.package_path("alpha", "1.1"))
        result = self.rrcc("--newest-only", self.tmp, rc=1)
        self.assertProblem(result, "MISSING: Packages/a/alpha-1.1-1.x86_64.rpm")

    def test_skipped_packages_are_not_orphans(self):
        standard_repo(self.tmp)
        self.assertConsistent(self.rrcc("-n", "--extra", self.tmp))

    def test_modular_repo(self):
        # The newest build (2) of the stream has an older a than build 1, so
        # both builds are kept, unlike what "latest a" would give. Of the
        # older b builds (3), only b-2 in build 4 is kept.
        repo = RepoBuilder(self.tmp)
        repo.add("a", "2", "1.module+1")
        repo.add("a", "1", "1.module+2")
        repo.add("b", "1", "1.module+3")
        repo.add("b", "2", "1.module+4")
        repo.add("plain", "1")
        repo.add("plain", "2")
        repo.add_module("m", "s", 1, ["a-0:2-1.module+1.x86_64"])
        repo.add_module("m", "s", 2, ["a-0:1-1.module+2.x86_64"])
        repo.add_module("n", "s", 3, ["b-0:1-1.module+3.x86_64"])
        repo.add_module("n", "s", 4, ["b-0:2-1.module+4.x86_64"])
        repo.build()
        result = self.rrcc("-n", "-v", self.tmp, rc=0)
        self.assertIn("skipped 2 older package(s), 6 in metadata", result.out)
        for kept in ("a/a-2-1.module+1", "a/a-1-1.module+2", "b/b-2-1.module+4", "p/plain-2-1"):
            self.assertIn(f"OK: Packages/{kept}.x86_64.rpm", result.out)

    def test_missing_modules_file_is_an_error(self):
        repo = RepoBuilder(self.tmp)
        repo.add("a", "1", "1.module+1")
        repo.add_module("m", "s", 1, ["a-0:1-1.module+1.x86_64"])
        repo.build()
        os.remove(repo.metadata["modules"])
        result = self.rrcc("-n", self.tmp, rc=2)
        self.assertIn("modules metadata file listed in repomd.xml is missing", result.out)


class DiscoveryTest(TestCase):
    def test_top_level(self):
        standard_repo(self.path("mirror", "os", "x86_64"))
        standard_repo(self.path("mirror", "updates", "x86_64"))
        os.makedirs(self.path("mirror", "empty"))
        result = self.rrcc("--top-level", self.path("mirror"), rc=0)
        self.assertIn("Found 2 repo(s) under", result.out)
        self.assertIn("=== os/x86_64 ===", result.out)
        self.assertIn("=== updates/x86_64 ===", result.out)
        self.assertIn("Summary: 2 repo(s), 12 package(s) checked total.", result.out)

    def test_top_level_problem_in_one_repo(self):
        standard_repo(self.path("mirror", "a"))
        repo = standard_repo(self.path("mirror", "b"))
        os.remove(repo.package_path("delta"))
        result = self.rrcc("--top-level", self.path("mirror"), rc=1)
        self.assertProblem(result, "MISSING: Packages/d/delta-5-1.x86_64.rpm")

    def test_top_level_nothing_found(self):
        result = self.rrcc("--top-level", self.tmp, rc=2)
        self.assertIn("no repos found under", result.err)

    def test_several_paths_and_duplicates(self):
        standard_repo(self.path("a"))
        standard_repo(self.path("b"))
        result = self.rrcc(self.path("a"), self.path("b"), self.path("a") + "/", rc=0)
        self.assertIn("Summary: 2 repo(s), 12 package(s) checked total.", result.out)


class ErrorTest(TestCase):
    def test_no_repodata(self):
        result = self.rrcc(self.tmp, rc=2)
        self.assertIn("ERROR: missing", result.out)
        self.assertIn("Result: one or more repos could not be read.", result.out)

    def test_bad_repomd(self):
        os.makedirs(self.path("repodata"))
        with open(self.path("repodata", "repomd.xml"), "w") as f:
            f.write("<repomd")
        self.assertIn("ParseError", self.rrcc(self.tmp, rc=2).out)

    def test_no_primary_entry(self):
        repo = standard_repo(self.tmp)
        repo.edit_repomd('type="primary"', 'type="not-primary"')
        self.assertIn('no <data type="primary"> entry', self.rrcc(self.tmp, rc=2).out)

    def test_one_broken_repo_does_not_stop_others(self):
        standard_repo(self.path("good"))
        os.makedirs(self.path("bad"))
        result = self.rrcc(self.path("bad"), self.path("good"), rc=2)
        self.assertIn("6 packages checked, consistent", result.out)


class CommandLineTest(TestCase):
    def test_version(self):
        result = self.rrcc("--version", rc=0)
        self.assertEqual(result.out.strip(), f"rrcc {load_rrcc().__version__}")

    def test_invalid_jobs(self):
        for value in ("0", "-1", "x"):
            with self.subTest(value=value):
                result = self.rrcc("-j", value, self.tmp, rc=2)
                self.assertIn("must be an integer >= 1", result.err)

    def test_no_path(self):
        self.rrcc(rc=2)


if __name__ == "__main__":
    unittest.main()
