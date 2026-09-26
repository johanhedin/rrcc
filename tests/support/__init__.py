"""Shared helpers for the rrcc test suite.

The tests only use the Python standard library, so they run on the same
Python versions as rrcc itself (3.6 and newer). Tests that need an optional
tool or module (unzck, zck, zstandard, python3-rpm, libmodulemd, dnf) are
skipped when it isn't installed.

Run the tests with 'make test' from the top of the repository, or with
'python3 -m unittest discover -s tests'.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RRCC = os.path.join(ROOT, "rrcc.py")
PKI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pki")

# Keep the source tree free of __pycache__ from importing rrcc.py
sys.dont_write_bytecode = True

_rrcc = None


def load_rrcc():
    """Import rrcc.py as the module 'rrcc', for unit tests."""
    global _rrcc
    if _rrcc is None:
        spec = importlib.util.spec_from_file_location("rrcc", RRCC)
        _rrcc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_rrcc)
    return _rrcc


# Variables that would change how rrcc behaves if they leaked in from the
# environment of whoever runs the tests.
_SCRUBBED_ENV = ("http_proxy", "https_proxy", "no_proxy", "all_proxy",
                 "SSL_CERT_FILE", "SSL_CERT_DIR")


class Result:
    def __init__(self, rc, out, err):
        self.rc = rc
        self.out = out
        self.err = err

    def __repr__(self):
        return f"<exit {self.rc}>\n--- stdout\n{self.out}--- stderr\n{self.err}"


def run_rrcc(*args, env=None, timeout=300):
    """Run rrcc.py in a subprocess with a clean environment plus env, and
    return a Result with the exit code and output."""
    full_env = {k: v for k, v in os.environ.items()
                if k not in _SCRUBBED_ENV and k.lower() not in _SCRUBBED_ENV}
    full_env["PYTHONDONTWRITEBYTECODE"] = "1"
    full_env.update(env or {})
    proc = subprocess.run([sys.executable, RRCC] + [str(a) for a in args],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, env=full_env, timeout=timeout)
    return Result(proc.returncode, proc.stdout, proc.stderr)


def have_command(name):
    return shutil.which(name) is not None


def have_module(name):
    try:
        importlib.import_module(name)
    except ImportError:
        return False
    return True


def have_zstd():
    return have_module("compression.zstd") or have_module("zstandard")


def have_modulemd():
    try:
        import gi
        gi.require_version("Modulemd", "2.0")
        from gi.repository import Modulemd  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


def slow_tests_enabled():
    return os.environ.get("RRCC_SLOW_TESTS") == "1"


class TestCase(unittest.TestCase):
    """TestCase with a per-test temporary directory and helpers for
    checking rrcc's output."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="rrcc-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def path(self, *parts):
        return os.path.join(self.tmp, *parts)

    def rrcc(self, *args, rc=None, env=None):
        """Run rrcc and, if rc is given, assert its exit code."""
        result = run_rrcc(*args, env=env)
        if rc is not None:
            self.assertEqual(result.rc, rc, result)
        return result

    def problems(self, result):
        """The problem lines of rrcc's report (the indented ones below
        'N packages checked, M problem(s):')."""
        return [line.strip() for line in result.out.splitlines() if line.startswith("    ")]

    def assertProblem(self, result, text):
        """Assert that some problem line contains text."""
        problems = self.problems(result)
        self.assertTrue(any(text in p for p in problems),
                        f"no problem containing {text!r} in:\n" + "\n".join(problems) + f"\n{result!r}")

    def assertNoProblem(self, result, text):
        problems = self.problems(result)
        self.assertFalse(any(text in p for p in problems),
                         f"unexpected problem containing {text!r}:\n{result!r}")

    def assertConsistent(self, result):
        self.assertEqual(result.rc, 0, result)
        self.assertIn("Result: all checked repos are consistent.", result.out)
