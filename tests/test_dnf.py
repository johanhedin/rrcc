"""Compare --newest-only with 'dnf reposync --newest-only' on random repos.

dnf 4 (RHEL 8-10) applies special rules to modular packages, which rrcc
follows by default. dnf 5 (Fedora 41+) ignores the modules, which rrcc does
with --ignore-modules. The test uses whichever dnf is installed.

This is slow (dnf takes about half a second per repo), so it only runs when
RRCC_SLOW_TESTS=1 is set ('make test-all'). RRCC_DNF_REPOS sets the number
of random repos (default 40).
"""

import os
import random
import re
import subprocess
import unittest

from support import TestCase, have_command, load_rrcc, slow_tests_enabled
from support.repo import RepoBuilder


def have_reposync():
    if not have_command("dnf"):
        return False
    proc = subprocess.run(["dnf", "reposync", "--help"], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL)
    return proc.returncode == 0


def dnf_major_version():
    """4 for dnf 4 ("4.14.0"), 5 for dnf 5 ("dnf5 version 5.2.18.0")."""
    proc = subprocess.run(["dnf", "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True)
    match = re.search(r"\b(\d+)\.\d+\.\d+", proc.stdout)
    if match is None:
        raise RuntimeError(f"cannot tell the dnf version from: {proc.stdout[:200]!r}")
    return int(match.group(1))


NAMES = ["foo", "bar", "baz", "qux", "lib"]
ARCHES = ["x86_64", "noarch"]
VERSIONS = ["1.0", "1.0.1", "1.1", "2", "2~rc1", "10", "1.a", "1^git"]
RELEASES = ["1", "2", "10", "1.el8", "1.module_x+1", "2.module_x+2", "1.module_x+3"]


def random_repo(root, rnd):
    """A repo with several versions of a few packages and random module
    builds that share or overlap their packages."""
    repo = RepoBuilder(root)
    seen = set()
    for _ in range(rnd.randint(10, 40)):
        evr = (rnd.choice(NAMES), rnd.choice(ARCHES), rnd.choice(["0", "0", "1"]),
               rnd.choice(VERSIONS), rnd.choice(RELEASES))
        if evr in seen:
            continue
        seen.add(evr)
        name, arch, epoch, version, release = evr
        repo.add(name, version, release, arch=arch, epoch=epoch,
                 href=f"Packages/{name}-{epoch}-{version}-{release}.{arch}.rpm")
    nevras = [RepoBuilder.nevra(p) for p in repo.packages]
    for i in range(rnd.randint(0, 8)):
        artifacts = rnd.sample(nevras, rnd.randint(0, min(8, len(nevras))))
        if rnd.random() < 0.3:
            artifacts.append("ghost-0:1-1.x86_64")  # not in the repo
        repo.add_module(rnd.choice(["m1", "m2", "m3"]), rnd.choice(["a", "b"]),
                        rnd.randint(1, 5), artifacts, context=f"c{i}")
    return repo.build()


@unittest.skipUnless(slow_tests_enabled(), "slow; set RRCC_SLOW_TESTS=1 (make test-all)")
@unittest.skipUnless(have_reposync(), "needs dnf with the reposync plugin (dnf-plugins-core)")
class DnfNewestOnlyTest(TestCase):
    def dnf_selection(self, root, repoid):
        proc = subprocess.run(
            ["dnf", "-q", f"--repofrompath={repoid},file://{root}", f"--repoid={repoid}",
             f"--setopt={repoid}.gpgcheck=0", f"--setopt={repoid}.metadata_expire=0",
             "reposync", "--newest-only", "--urls"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        return sorted(line.rsplit("/", 1)[1] for line in proc.stdout.splitlines() if line.endswith(".rpm"))

    def rrcc_selection(self, root, rrcc_args):
        result = self.rrcc("--newest-only", "-v", *rrcc_args, root, rc=0)
        return sorted(line.rsplit("/", 1)[1] for line in result.out.splitlines()
                      if line.startswith("  OK: Packages/"))

    def test_same_as_dnf(self):
        count = int(os.environ.get("RRCC_DNF_REPOS", "40"))
        strict_subsets = 0
        module_rules_mattered = 0
        rrcc = load_rrcc()
        rrcc_args = ["--ignore-modules"] if dnf_major_version() >= 5 else []
        for seed in range(count):
            with self.subTest(seed=seed):
                root = self.path(f"repo{seed}")
                repo = random_repo(root, random.Random(seed))
                expected = self.dnf_selection(root, f"rrcctest{seed}")
                self.assertEqual(self.rrcc_selection(root, rrcc_args), expected)
                strict_subsets += len(expected) < len(repo.packages)
                packages = list(rrcc.iter_packages(repo.metadata["primary"]))
                modules = rrcc.read_modules(repo.metadata["modules"]) if repo.modules else []
                module_rules_mattered += (rrcc.newest_packages(packages, modules)
                                          != rrcc.latest_packages(packages))
        # make sure the random repos exercise the selection, and repos where
        # the module rules make a difference (for dnf 5: where ignoring them
        # does)
        self.assertGreater(strict_subsets, count // 2)
        self.assertGreater(module_rules_mattered, count // 2)


if __name__ == "__main__":
    unittest.main()
