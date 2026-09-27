"""Tests for -h, the short help, and --help, the full help, which list
each option once."""

import re
import unittest

from support import TestCase

# Help is wrapped to the terminal width, so make it the same everywhere
ENV = {"COLUMNS": "80"}

OPTIONS = ("--top-level", "--checksum", "--extra", "--max-age DAYS", "--newest-only",
           "--ignore-modules", "--jobs N", "--quiet", "--verbose", "--no-progress",
           "--version", "--ca-cert FILE", "--client-cert FILE", "--client-key FILE",
           "--insecure", "--no-strict-x509")


class HelpTest(TestCase):
    def assertEachOptionOnce(self, out):
        for option in OPTIONS:
            entries = re.findall(rf"^  (?:-\w+(?: \w+)?, )?{re.escape(option)}(?:  |$)", out, re.M)
            self.assertEqual(len(entries), 1, f"{option!r} listed {len(entries)} times in:\n{out}")

    def test_short_help(self):
        result = self.rrcc("-h", rc=0, env=ENV)
        self.assertEachOptionOnce(result.out)
        self.assertIn("RPM Repository Consistency Checker", result.out)
        self.assertIn("also verify checksums (slow)", result.out)
        self.assertIn("Use --help for the full help", result.out)
        self.assertNotIn("Exit status:", result.out)
        self.assertNotIn("Multiple repos under a common parent:", result.out)
        self.assertLess(len(result.out.splitlines()), 60)

    def test_full_help(self):
        result = self.rrcc("--help", rc=0, env=ENV)
        self.assertEachOptionOnce(result.out)
        self.assertIn("Multiple repos under a common parent:", result.out)
        self.assertIn("Proxy:", result.out)
        self.assertIn("Exit status:", result.out)
        self.assertIn("keeps its own", result.out)
        self.assertIn("/etc/rhsm/ca/redhat-uep.pem", result.out)
        self.assertNotIn("Use --help for the full help", result.out)
        self.assertLess(len(result.out.splitlines()), 150)

    def test_help_ignores_other_arguments(self):
        result = self.rrcc("--checksum", "--help", "/nonexistent", rc=0, env=ENV)
        self.assertIn("Exit status:", result.out)

    def test_no_arguments(self):
        result = self.rrcc(rc=2, env=ENV)
        self.assertIn("usage:", result.err)
        self.assertIn("required: path", result.err)


if __name__ == "__main__":
    unittest.main()
