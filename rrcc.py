#!/usr/bin/env python3
"""
RPM Repository Consistency Checker, or rrcc for short.

Verify that a local RPM repository mirror is consistent with its own
repodata: every package listed in primary.xml actually exists on disk
(and, optionally, matches the recorded size/checksum).

Single repo:
    rrcc.py [options] /path/to/repo/root

    /path/to/repo/root is the directory that CONTAINS "repodata/"
    (i.e. the same directory you'd point a baseurl at).

Multiple repos under a common parent:
    rrcc.py --top-level [options] /path/to/mirrors

    Recursively finds every directory under the given path that has a
    "repodata/repomd.xml" in it, and checks each one as a separate repo.
    Useful when you mirror several release/arch trees (or os/debug/source
    variants) under one parent directory.

Options:
    --top-level          Treat the path as a parent directory containing
                         multiple repos; auto-discover and check each one.
    --checksum           Verify checksums too (slow: reads every RPM).
                         Without this flag, only presence + size are checked.
    --extra              Also report *.rpm files on disk that are NOT
                         referenced by primary.xml (orphans / stale files).
    -v, --verbose        Print a line for every package checked.
    --version            Print the version and exit.

If repomd.xml also lists a "primary_zck" entry (the zchunk-compressed copy
of primary.xml that createrepo_c --zck produces), it is cross-checked against
the plain primary: it must exist, be readable, and list exactly the same
packages with the same sizes and checksums. This needs 'unzck' (see below),
and is reported as a problem if the file can't be read.

Exit status:
    0   everything checked is consistent
    1   at least one problem found in at least one repo
    2   couldn't even read repodata for one or more repos (or none found
        under --top-level)

Uses only the Python standard library, with two exceptions for
primary.xml compression formats that Python can't read by itself:

    .zst   (the default on modern Fedora/createrepo_c repos) needs either
           Python 3.14+ (stdlib compression.zstd) or the third-party
           'zstandard' package.
    .zck   (zchunk) needs the external 'unzck' command from the 'zchunk'
           package, which must be in PATH. On Rocky/RHEL 9 this is
           "dnf install zchunk" and requires EPEL to be enabled. This works
           on any Python version.

.gz and .xz are always handled via the stdlib.
"""

import argparse
import gzip
import hashlib
import lzma
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zlib

__version__ = "1.0.0"

NS_REPO = "{http://linux.duke.edu/metadata/repo}"
NS_COMMON = "{http://linux.duke.edu/metadata/common}"
NS_RPM = "{http://linux.duke.edu/metadata/rpm}"

CHECKSUM_ALGO_MAP = {
    "sha256": hashlib.sha256,
    "sha1": hashlib.sha1,
    "sha": hashlib.sha1,
    "sha512": hashlib.sha512,
    "md5": hashlib.md5,
}


def open_maybe_compressed(path):
    if path.endswith(".gz"):
        return gzip.open(path, "rb")
    if path.endswith(".xz"):
        return lzma.open(path, "rb")
    if path.endswith(".zst"):
        return open_zstd(path)
    if path.endswith(".zck"):
        return ZckStream(path)
    return open(path, "rb")


class ZckStream:
    """Read-only file-like object yielding the decompressed contents of a
    .zck (zchunk) file. Python has no zchunk support, so this streams the
    output of the external 'unzck --stdout' command (package 'zchunk',
    from EPEL on Rocky/RHEL 8+). If unzck fails, the error is raised from
    read() when the end of the stream is reached."""

    def __init__(self, path):
        self.path = path
        unzck = shutil.which("unzck")
        if unzck is None:
            raise RuntimeError(
                f"{path}: .zck primary metadata requires the 'unzck' command "
                "(dnf install zchunk; on Rocky/RHEL 8+ this needs EPEL)"
            )
        self._stderr = tempfile.TemporaryFile()
        try:
            self._proc = subprocess.Popen([unzck, "--stdout", path],
                                          stdout=subprocess.PIPE, stderr=self._stderr)
        except BaseException:
            self._stderr.close()
            raise

    def read(self, size=-1):
        data = self._proc.stdout.read(size)
        if not data or size is None or size < 0:
            self._check_exit()
        return data

    def _check_exit(self):
        rc = self._proc.wait()
        if rc != 0:
            self._stderr.seek(0)
            msg = self._stderr.read().decode(errors="replace").strip()
            raise RuntimeError(f"{self.path}: unzck failed (exit status {rc})"
                               + (f": {msg}" if msg else ""))

    def close(self):
        if self._proc.poll() is None:
            self._proc.kill()  # abandoned before EOF
        self._proc.stdout.close()
        self._proc.wait()
        self._stderr.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def open_zstd(path):
    """Open a .zst file for reading, using the stdlib compression.zstd
    module (Python 3.14+) if available, else falling back to the
    third-party 'zstandard' package."""
    try:
        from compression import zstd
    except ImportError:
        pass
    else:
        return zstd.open(path, "rb")

    try:
        import zstandard
    except ImportError as e:
        raise RuntimeError(
            f"{path}: .zst primary metadata requires either Python 3.14+ "
            "(stdlib compression.zstd) or the 'zstandard' package "
            "(pip install zstandard / dnf install python3-zstandard)"
        ) from e

    fh = open(path, "rb")
    return zstandard.ZstdDecompressor().stream_reader(fh, closefd=True)


def find_data_location(repo_root, data_type, required=True):
    """Return the href of the <data type="..."> entry in repomd.xml.
    If there is no such entry, raise RuntimeError, or return None when
    required is False."""
    repomd_path = os.path.join(repo_root, "repodata", "repomd.xml")
    if not os.path.isfile(repomd_path):
        raise RuntimeError(f"missing {repomd_path}")

    tree = ET.parse(repomd_path)
    root = tree.getroot()
    for data_el in root.findall(f"{NS_REPO}data"):
        if data_el.get("type") == data_type:
            loc = data_el.find(f"{NS_REPO}location")
            if loc is None or "href" not in loc.attrib:
                raise RuntimeError(f"{data_type} <location> missing href in repomd.xml")
            return loc.attrib["href"]
    if required:
        raise RuntimeError(f'no <data type="{data_type}"> entry found in repomd.xml')
    return None


def iter_packages(primary_path):
    """Yield dicts: {href, size, checksum_type, checksum}"""
    with open_maybe_compressed(primary_path) as fh:
        for event, elem in ET.iterparse(fh, events=("end",)):
            if elem.tag != f"{NS_COMMON}package":
                continue
            loc = elem.find(f"{NS_COMMON}location")
            size_el = elem.find(f"{NS_COMMON}size")
            csum_el = elem.find(f"{NS_COMMON}checksum")
            if loc is None or "href" not in loc.attrib:
                elem.clear()
                continue
            yield {
                "href": loc.attrib["href"],
                "size": int(size_el.attrib["package"]) if size_el is not None else None,
                "checksum_type": csum_el.attrib.get("type") if csum_el is not None else None,
                "checksum": csum_el.text if csum_el is not None else None,
            }
            elem.clear()


def verify_checksum(path, algo_name, expected_hex):
    hasher = CHECKSUM_ALGO_MAP.get(algo_name.lower())
    if hasher is None:
        return None  # unknown algo, skip
    h = hasher()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest() == expected_hex


def find_repos(top_level):
    """Recursively find directories under top_level that contain a valid
    repodata/repomd.xml. Does not descend into repodata/ itself."""
    repos = []
    for dirpath, dirnames, _filenames in os.walk(top_level):
        if "repodata" in dirnames:
            if os.path.isfile(os.path.join(dirpath, "repodata", "repomd.xml")):
                repos.append(dirpath)
            # never need to walk into a repodata dir looking for more repos
            dirnames.remove("repodata")
    return sorted(repos)


# Failures that mean "this repo's metadata (or a file it points at) can't
# be read", as opposed to a package problem. Reported per repo so that one
# broken repo doesn't stop the others from being checked.
READ_ERRORS = (RuntimeError, ET.ParseError, OSError, EOFError,
               lzma.LZMAError, zlib.error, ValueError)


def check_one_repo(repo_root, args):
    """Returns (status, count, problems, notes).
    status is one of 'ok', 'problems', 'error'.
    On 'error', problems is a single-element list with the error message.
    notes is a list of informational lines about what else was checked.
    """
    try:
        return _check_one_repo(repo_root, args)
    except READ_ERRORS as e:
        return "error", 0, [f"{type(e).__name__}: {e}"], []


def _compare_primary_zck(repo_root, zck_href, plain):
    """Cross-check the primary_zck metadata against the plain primary.
    plain maps normalized href -> (size, checksum_type, checksum) as read
    from the plain primary. Returns (problems, note)."""
    zck_path = os.path.join(repo_root, zck_href)
    if not os.path.isfile(zck_path):
        return [f"PRIMARY_ZCK MISSING: listed in repomd.xml but not on disk: {zck_href}"], None

    problems = []
    zck_seen = set()
    try:
        for pkg in iter_packages(zck_path):
            href = os.path.normpath(pkg["href"])
            zck_seen.add(href)
            if href not in plain:
                problems.append(f"PRIMARY_ZCK EXTRA: in primary_zck but not in primary: {pkg['href']}")
            elif plain[href] != (pkg["size"], pkg["checksum_type"], pkg["checksum"]):
                problems.append(f"PRIMARY_ZCK MISMATCH: size/checksum differs from primary: {pkg['href']}")
    except READ_ERRORS as e:
        return [f"PRIMARY_ZCK UNREADABLE: {zck_href}: {type(e).__name__}: {e}"], None

    for href in sorted(set(plain) - zck_seen):
        problems.append(f"PRIMARY_ZCK MISSING PACKAGE: in primary but not in primary_zck: {href}")

    return problems, f"primary_zck cross-checked against primary ({len(zck_seen)} packages)"


def _check_one_repo(repo_root, args):
    try:
        primary_href = find_data_location(repo_root, "primary")
    except RuntimeError as e:
        return "error", 0, [str(e)], []

    primary_path = os.path.join(repo_root, primary_href)
    if not os.path.isfile(primary_path):
        return "error", 0, [f"primary metadata file listed in repomd.xml is missing: {primary_path}"], []

    zck_href = find_data_location(repo_root, "primary_zck", required=False)

    problems = []
    notes = []
    seen_hrefs = set()
    plain = {}  # normalized href -> (size, checksum_type, checksum), for the primary_zck cross-check
    count = 0

    for pkg in iter_packages(primary_path):
        count += 1
        href = pkg["href"]
        seen_hrefs.add(os.path.normpath(href))
        plain[os.path.normpath(href)] = (pkg["size"], pkg["checksum_type"], pkg["checksum"])
        full_path = os.path.join(repo_root, href)

        if not os.path.isfile(full_path):
            problems.append(f"MISSING: {href}")
            continue

        if pkg["size"] is not None:
            actual_size = os.path.getsize(full_path)
            if actual_size != pkg["size"]:
                problems.append(f"SIZE MISMATCH: {href} (expected {pkg['size']}, got {actual_size})")
                continue  # no point checksumming a truncated/corrupt file

        if args.checksum and pkg["checksum_type"] and pkg["checksum"]:
            ok = verify_checksum(full_path, pkg["checksum_type"], pkg["checksum"])
            if ok is False:
                problems.append(f"CHECKSUM MISMATCH: {href}")
                continue
            elif ok is None:
                problems.append(f"WARNING: unsupported checksum algo '{pkg['checksum_type']}' for {href}, skipped")

        if args.verbose:
            print(f"  OK: {href}")

    if zck_href is not None:
        zck_problems, note = _compare_primary_zck(repo_root, zck_href, plain)
        problems.extend(zck_problems)
        if note:
            notes.append(note)

    if args.extra:
        for dirpath, _, filenames in os.walk(repo_root):
            if os.sep + "repodata" in dirpath + os.sep:
                continue
            for fn in filenames:
                if not fn.endswith(".rpm"):
                    continue
                rel = os.path.normpath(os.path.relpath(os.path.join(dirpath, fn), repo_root))
                if rel not in seen_hrefs:
                    problems.append(f"ORPHAN (on disk, not in metadata): {rel}")

    status = "problems" if problems else "ok"
    return status, count, problems, notes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", metavar="path",
                    help="repo root(s), or (with --top-level) parent directories of multiple repos")
    ap.add_argument("--top-level", action="store_true",
                     help="treat each 'path' as a parent dir; auto-discover repos under it")
    ap.add_argument("--checksum", action="store_true", help="also verify checksums (slow)")
    ap.add_argument("--extra", action="store_true", help="report on-disk RPMs not in metadata")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"rrcc {__version__}",
                    help="print the version and exit")
    args = ap.parse_args()

    overall_problem = False
    overall_error = False
    total_packages = 0

    # (label, repo_root) pairs, in command-line order, without duplicates
    repos = []
    seen_roots = set()

    def add_repo(label, root):
        if root not in seen_roots:
            seen_roots.add(root)
            repos.append((label, root))

    for path in args.paths:
        top_path = os.path.abspath(path)
        if args.top_level:
            found = find_repos(top_path)
            if not found:
                print(f"ERROR: no repos found under {top_path} (looked for */repodata/repomd.xml)", file=sys.stderr)
                overall_error = True
                continue
            print(f"Found {len(found)} repo(s) under {top_path}:")
            for r in found:
                print(f"  {os.path.relpath(r, top_path)}")
                add_repo(r if len(args.paths) > 1 else os.path.relpath(r, top_path), r)
            print()
        else:
            add_repo(top_path, top_path)

    for label, repo_root in repos:
        print(f"=== {label} ===")
        status, count, problems, notes = check_one_repo(repo_root, args)
        total_packages += count

        if status == "error":
            overall_error = True
            print(f"  ERROR: {problems[0]}")
        elif status == "problems":
            overall_problem = True
            print(f"  {count} packages checked, {len(problems)} problem(s):")
            for p in problems:
                print(f"    {p}")
        else:
            print(f"  {count} packages checked, consistent"
                  + (" (checksums verified)." if args.checksum else " (size-checked)."))
        for note in notes:
            print(f"  {note}")
        print()

    print(f"Summary: {len(repos)} repo(s), {total_packages} package(s) checked total.")
    if overall_error:
        print("Result: one or more repos could not be read.")
        return 2
    if overall_problem:
        print("Result: inconsistencies found.")
        return 1
    print("Result: all checked repos are consistent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

