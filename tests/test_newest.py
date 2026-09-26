"""Tests for the package selection of --newest-only and for reading
modules.yaml."""

import collections
import gzip
import os
import unittest

from support import TestCase, have_modulemd, load_rrcc
from support.repo import RepoBuilder

rrcc = load_rrcc()


def pkg(name, version, release="1", arch="x86_64", epoch="0"):
    return {"name": name, "version": version, "release": release, "arch": arch,
            "epoch": epoch, "href": f"{name}-{version}-{release}.{arch}.rpm"}


def nevra(p):
    return f"{p['name']}-{p['epoch']}:{p['version']}-{p['release']}.{p['arch']}"


def module(name, stream, version, artifacts):
    return {"name": name, "stream": stream, "version": version,
            "artifacts": {nevra(p) if isinstance(p, dict) else p for p in artifacts}}


def hrefs(packages):
    return [p["href"] for p in packages]


class LatestPackagesTest(unittest.TestCase):
    def test_keeps_latest_per_name_and_arch(self):
        old, new = pkg("foo", "1.0"), pkg("foo", "1.1")
        noarch = pkg("foo", "1.0", arch="noarch")
        bar = pkg("bar", "3")
        self.assertEqual(rrcc.latest_packages([old, noarch, new, bar]), [noarch, new, bar])

    def test_keeps_metadata_order(self):
        packages = [pkg("b", "2"), pkg("a", "1"), pkg("b", "1"), pkg("a", "2")]
        self.assertEqual(hrefs(rrcc.latest_packages(packages)),
                         ["b-2-1.x86_64.rpm", "a-2-1.x86_64.rpm"])

    def test_keeps_all_with_the_highest_version(self):
        # Same NEVRA twice, e.g. at two locations in the repo
        a, b = pkg("foo", "1.0"), pkg("foo", "1.0")
        b["href"] = "other/" + b["href"]
        self.assertEqual(rrcc.latest_packages([a, b, pkg("foo", "0.9")]), [a, b])

    def test_epoch_beats_version(self):
        high_epoch = pkg("foo", "1.0", epoch="1")
        self.assertEqual(rrcc.latest_packages([high_epoch, pkg("foo", "9.0")]), [high_epoch])

    def test_rpm_ordering(self):
        rc, final, snapshot = pkg("foo", "2~rc1"), pkg("foo", "2"), pkg("foo", "2^git1")
        self.assertEqual(rrcc.latest_packages([snapshot, rc, final]), [snapshot])
        self.assertEqual(rrcc.latest_packages([rc, final]), [final])

    def test_keeps_packages_without_name_or_version(self):
        odd = {"name": None, "version": None, "release": None, "arch": None,
               "epoch": None, "href": "odd.rpm"}
        self.assertEqual(rrcc.latest_packages([odd, pkg("foo", "1")]), [odd, pkg("foo", "1")])


class NewestPackagesTest(unittest.TestCase):
    def test_without_modules_is_latest(self):
        packages = [pkg("foo", "1"), pkg("foo", "2"), pkg("bar", "1")]
        self.assertEqual(rrcc.newest_packages(packages), rrcc.latest_packages(packages))

    def test_modular_and_non_modular_are_separate(self):
        # A newer non-modular foo doesn't hide the modular one, nor the reverse
        plain = pkg("foo", "2")
        modular = pkg("foo", "1", "1.module+1")
        mods = [module("m", "s", 1, [modular])]
        self.assertEqual(rrcc.newest_packages([plain, modular], mods), [plain, modular])

    def test_all_packages_of_newest_module_build(self):
        # The newest build of the stream is kept entirely, even a package
        # that is older than one in an older build.
        old_a, old_b = pkg("a", "2", "1.module+1"), pkg("b", "1", "1.module+1")
        new_a, new_b = pkg("a", "1", "1.module+2"), pkg("b", "2", "1.module+2")
        mods = [module("m", "s", 1, [old_a, old_b]), module("m", "s", 2, [new_a, new_b])]
        # a-2 is the latest a and only in build 1, so build 1 is kept too
        self.assertEqual(rrcc.newest_packages([old_a, old_b, new_a, new_b], mods),
                         [old_a, old_b, new_a, new_b])

    def test_older_module_build_dropped(self):
        old = pkg("a", "1", "1.module+1")
        new = pkg("a", "2", "1.module+2")
        mods = [module("m", "s", 1, [old]), module("m", "s", 2, [new])]
        self.assertEqual(rrcc.newest_packages([old, new], mods), [new])

    def test_streams_are_separate(self):
        s1 = pkg("a", "1", "1.module+1")
        s2 = pkg("a", "2", "1.module+2")
        mods = [module("m", "one", 1, [s1]), module("m", "two", 1, [s2])]
        self.assertEqual(rrcc.newest_packages([s1, s2], mods), [s1, s2])

    def test_artifacts_not_in_primary_are_ignored(self):
        a = pkg("a", "1", "1.module+1")
        mods = [module("m", "s", 1, [a, "ghost-0:1-1.x86_64"])]
        self.assertEqual(rrcc.newest_packages([a], mods), [a])


class ReadModulesTest(TestCase):
    def build(self):
        repo = RepoBuilder(self.tmp)
        repo.add("a", "1", "1.module+1")
        repo.add("a", "2", "1.module+2")
        repo.add_module("m", "s", 20260101, ["a-0:1-1.module+1.x86_64"], context="c1")
        repo.add_module("m", "s", 20260202, ["a-0:2-1.module+2.x86_64"], context="c1")
        repo.add_module("m", "other", 3, [])
        repo.build()
        return repo

    def test_read_modules(self):
        repo = self.build()
        modules = rrcc.read_modules(repo.metadata["modules"])
        self.assertEqual(
            sorted((m["name"], m["stream"], m["version"], sorted(m["artifacts"])) for m in modules),
            [("m", "other", 3, []),
             ("m", "s", 20260101, ["a-0:1-1.module+1.x86_64"]),
             ("m", "s", 20260202, ["a-0:2-1.module+2.x86_64"])])

    def test_skips_other_documents(self):
        path = self.path("modules.yaml")
        with open(path, "w") as f:
            f.write("---\ndocument: modulemd-defaults\nversion: 1\ndata:\n"
                    "  module: m\n  stream: s\n...\n"
                    "---\ndocument: modulemd\nversion: 2\ndata:\n"
                    "  name: m\n  stream: 's'\n  version: 5\n"
                    "  artifacts:\n    rpms:\n    - a-0:1-1.x86_64\n...\n")
        modules = rrcc.read_modules(path)
        self.assertEqual([(m["name"], m["stream"], m["version"], m["artifacts"]) for m in modules],
                         [("m", "s", 5, {"a-0:1-1.x86_64"})])

    def test_missing_version_is_an_error(self):
        path = self.path("modules.yaml")
        with open(path, "w") as f:
            f.write("---\ndocument: modulemd\nversion: 2\ndata:\n  name: m\n  stream: s\n...\n")
        with self.assertRaises(RuntimeError):
            rrcc.read_modules(path)

    @unittest.skipUnless(have_modulemd(), "libmodulemd (python3-libmodulemd) is not installed")
    def test_same_as_libmodulemd(self):
        import gi
        gi.require_version("Modulemd", "2.0")
        from gi.repository import Modulemd

        repo = self.build()
        plain = self.path("modules.yaml")
        with gzip.open(repo.metadata["modules"]) as src, open(plain, "wb") as dst:
            dst.write(src.read())
        index = Modulemd.ModuleIndex.new()
        ok, failures = index.update_from_file(plain, True)
        self.assertTrue(ok and not failures, failures)

        expected = collections.Counter()
        for name in index.get_module_names():
            for stream in index.get_module(name).get_all_streams():
                expected[(stream.get_module_name(), stream.get_stream_name(), stream.get_version(),
                          frozenset(stream.get_rpm_artifacts()))] += 1
        got = collections.Counter((m["name"], m["stream"], m["version"], frozenset(m["artifacts"]))
                                  for m in rrcc.read_modules(repo.metadata["modules"]))
        self.assertEqual(got, expected)


if __name__ == "__main__":
    unittest.main()
