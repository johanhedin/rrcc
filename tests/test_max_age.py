"""Tests for --max-age, which reports a repository whose repomd.xml is
older than a number of days."""

import re
import time
import unittest

from support import TestCase
from support.repo import standard_repo
from support.servers import StaticServer

DAY = 86400


def set_ages(repo, *ages):
    """Set the <timestamp> of the entries in repomd.xml to now minus the
    given ages in seconds: the first age for the first entry and so on, and
    the last age for any remaining entries."""
    now = int(time.time())
    with open(repo.repomd) as f:
        text = f.read()
    stamps = iter(ages)
    last = [ages[-1]]

    def replace(_match):
        last[0] = next(stamps, last[0])
        return f"<timestamp>{now - last[0]}</timestamp>"

    with open(repo.repomd, "w") as f:
        f.write(re.sub(r"<timestamp>\d+</timestamp>", replace, text))


class MaxAgeTest(TestCase):
    def setUp(self):
        super().setUp()
        self.repo = standard_repo(self.tmp)

    def test_fresh(self):
        set_ages(self.repo, DAY)
        result = self.rrcc("--max-age", "7", self.tmp)
        self.assertConsistent(result)
        self.assertIn("repomd.xml is 1.0 days old (newest timestamp ", result.out)

    def test_stale(self):
        set_ages(self.repo, 10 * DAY)
        result = self.rrcc("--max-age", "7", self.tmp, rc=1)
        problems = self.problems(result)
        self.assertTrue(problems[0].startswith("STALE: repomd.xml is 10.0 days old (newest timestamp "),
                        problems)
        self.assertIn("--max-age 7)", problems[0])

    def test_stale_is_listed_before_other_problems(self):
        set_ages(self.repo, 10 * DAY)
        path = self.repo.package_path("alpha", "1.0")
        with open(path, "ab") as f:
            f.write(b"x")
        problems = self.problems(self.rrcc("--max-age", "7", self.tmp, rc=1))
        self.assertTrue(problems[0].startswith("STALE:"), problems)
        self.assertTrue(problems[1].startswith("SIZE MISMATCH:"), problems)

    def test_newest_timestamp_counts(self):
        set_ages(self.repo, 30 * DAY, DAY, 30 * DAY)
        self.assertConsistent(self.rrcc("--max-age", "7", self.tmp))

    def test_fraction(self):
        set_ages(self.repo, 3 * 3600)
        self.assertProblem(self.rrcc("--max-age", "0.1", self.tmp, rc=1), "STALE:")
        self.assertConsistent(self.rrcc("--max-age", "0.5", self.tmp))

    def test_future_timestamp(self):
        set_ages(self.repo, -DAY)
        result = self.rrcc("--max-age", "7", self.tmp)
        self.assertConsistent(result)
        self.assertIn("repomd.xml timestamp is in the future", result.out)

    def test_no_timestamp(self):
        with open(self.repo.repomd) as f:
            text = f.read()
        with open(self.repo.repomd, "w") as f:
            f.write(re.sub(r"<timestamp>\d+</timestamp>", "", text))
        self.assertProblem(self.rrcc("--max-age", "7", self.tmp, rc=1), "NO TIMESTAMP:")
        self.assertConsistent(self.rrcc(self.tmp))

    def test_invalid_timestamp_ignored_without_max_age(self):
        self.repo.edit_repomd("<timestamp>1700000000</timestamp>", "<timestamp>soon</timestamp>")
        self.assertConsistent(self.rrcc(self.tmp))

    def test_old_repo_is_fine_without_max_age(self):
        # the builder's timestamps are from 2023
        result = self.rrcc(self.tmp)
        self.assertConsistent(result)
        self.assertNotIn("days old", result.out)

    def test_invalid_values(self):
        for value in ("0", "-1", "abc", "nan", "inf"):
            with self.subTest(value=value):
                result = self.rrcc("--max-age", value, self.tmp, rc=2)
                self.assertIn("must be a number > 0", result.err)


class HttpMaxAgeTest(TestCase):
    def test_stale_over_http(self):
        repo = standard_repo(self.tmp)
        set_ages(repo, 10 * DAY)
        server = StaticServer(self.tmp).start()
        self.addCleanup(server.stop)
        self.assertProblem(self.rrcc("--max-age", "7", server.url, rc=1), "STALE:")
        self.assertConsistent(self.rrcc("--max-age", "11", server.url))


if __name__ == "__main__":
    unittest.main()
