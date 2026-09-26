"""Tests for the verification of the files listed in repomd.xml, on local
repositories. test_http.py covers the remote variant."""

import os
import unittest

from support import TestCase
from support.repo import flip_byte, standard_repo, truncate


class MetadataTest(TestCase):
    def setUp(self):
        super().setUp()
        self.repo = standard_repo(self.tmp)

    def test_clean(self):
        result = self.rrcc(self.tmp, rc=0)
        self.assertIn("4 metadata file(s) in repomd.xml checked (size and checksum)", result.out)

    def test_truncated(self):
        truncate(self.repo.metadata["filelists"], 100)
        result = self.rrcc(self.tmp, rc=1)
        self.assertProblem(result, "METADATA SIZE MISMATCH: repodata/")
        self.assertProblem(result, "-filelists.xml.gz (expected ")
        self.assertProblem(result, "(filelists)")

    def test_same_size_but_corrupt(self):
        flip_byte(self.repo.metadata["other"])
        result = self.rrcc(self.tmp, rc=1)
        self.assertProblem(result, "METADATA CHECKSUM MISMATCH: repodata/")
        self.assertProblem(result, "-other.xml.gz (other)")

    def test_missing(self):
        os.remove(self.repo.metadata["updateinfo"])
        result = self.rrcc(self.tmp, rc=1)
        self.assertProblem(result, "METADATA MISSING: repodata/")
        self.assertProblem(result, "-updateinfo.xml.xz (updateinfo)")

    def test_problems_listed_before_packages(self):
        os.remove(self.repo.metadata["updateinfo"])
        os.remove(self.repo.package_path("alpha", "1.0"))
        problems = self.problems(self.rrcc(self.tmp, rc=1))
        self.assertTrue(problems[0].startswith("METADATA MISSING"), problems)
        self.assertTrue(problems[1].startswith("MISSING: Packages/"), problems)

    def test_corrupt_primary_is_an_error(self):
        flip_byte(self.repo.metadata["primary"])
        result = self.rrcc(self.tmp, rc=2)
        self.assertIn("ERROR: primary metadata file does not match repomd.xml: CHECKSUM MISMATCH", result.out)

    def test_truncated_primary_is_an_error(self):
        truncate(self.repo.metadata["primary"])
        result = self.rrcc(self.tmp, rc=2)
        self.assertIn("ERROR: primary metadata file does not match repomd.xml: SIZE MISMATCH", result.out)

    def test_missing_primary_is_an_error(self):
        os.remove(self.repo.metadata["primary"])
        result = self.rrcc(self.tmp, rc=2)
        self.assertIn("primary metadata file listed in repomd.xml is missing", result.out)

    def test_unsupported_algorithm(self):
        self.repo.edit_repomd('<data type="other">\n    <checksum type="sha256">',
                              '<data type="other">\n    <checksum type="sha3_999">')
        result = self.rrcc(self.tmp, rc=1)
        self.assertProblem(result, "METADATA WARNING: unsupported checksum algo 'sha3_999'")

    def test_listed_twice_checked_once(self):
        with open(self.repo.repomd) as f:
            text = f.read()
        start = text.index('  <data type="other">')
        end = text.index("</data>", start) + len("</data>")
        dup = text[start:end].replace('type="other"', 'type="other_copy"')
        with open(self.repo.repomd, "w") as f:
            f.write(text[:end] + "\n" + dup + text[end:])
        result = self.rrcc(self.tmp, rc=0)
        self.assertIn("4 metadata file(s) in repomd.xml checked", result.out)


class NoSizeTest(TestCase):
    def test_checksum_used_when_size_missing(self):
        repo = standard_repo(self.tmp, drop_sizes=True)
        self.assertConsistent(self.rrcc(self.tmp))
        flip_byte(repo.metadata["other"])
        self.assertProblem(self.rrcc(self.tmp, rc=1), "METADATA CHECKSUM MISMATCH")


if __name__ == "__main__":
    unittest.main()
