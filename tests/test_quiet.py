"""Tests for --quiet, which prints only the repositories with problems and
nothing at all when every repository is consistent."""

import os
import unittest

from support import TestCase
from support.repo import standard_repo


def corrupt(repo):
    with open(repo.package_path("alpha", "1.0"), "ab") as f:
        f.write(b"x")


class QuietTest(TestCase):
    def test_consistent_prints_nothing(self):
        standard_repo(self.tmp)
        for option in ("-q", "--quiet"):
            with self.subTest(option=option):
                result = self.rrcc(option, "--checksum", self.tmp, rc=0)
                self.assertEqual(result.out, "")
                self.assertEqual(result.err, "")

    def test_problems(self):
        corrupt(standard_repo(self.tmp))
        result = self.rrcc("-q", self.tmp, rc=1)
        self.assertProblem(result, "SIZE MISMATCH:")
        self.assertIn(f"=== {self.tmp} ===", result.out)
        self.assertIn("Result: inconsistencies found.", result.out)
        self.assertNotIn("Summary:", result.out)
        self.assertNotIn("metadata file(s) in repomd.xml checked", result.out)

    def test_top_level_shows_only_bad_repos(self):
        standard_repo(self.path("mirror", "good"))
        corrupt(standard_repo(self.path("mirror", "bad")))
        result = self.rrcc("-q", "--top-level", self.path("mirror"), rc=1)
        self.assertNotIn("Found", result.out)
        self.assertIn("=== bad ===", result.out)
        self.assertNotIn("=== good ===", result.out)
        self.assertProblem(result, "SIZE MISMATCH:")

    def test_unreadable_repo(self):
        repo = standard_repo(self.tmp)
        os.remove(repo.repomd)
        result = self.rrcc("-q", self.tmp, rc=2)
        self.assertIn("ERROR: missing ", result.out)
        self.assertIn("Result: one or more repos could not be read.", result.out)

    def test_not_with_verbose(self):
        result = self.rrcc("-q", "-v", self.tmp, rc=2)
        self.assertIn("not allowed with argument", result.err)


if __name__ == "__main__":
    unittest.main()
