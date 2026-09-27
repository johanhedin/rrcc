# RPM Repository Consistency Checker

[![CI](https://github.com/johanhedin/rrcc/actions/workflows/ci.yml/badge.svg)](https://github.com/johanhedin/rrcc/actions/workflows/ci.yml)
[![CodeQL](https://github.com/johanhedin/rrcc/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/johanhedin/rrcc/security/code-scanning)

This repository contains a Python script called `rrcc`, which stands
for "RPM Repository Consistency Checker".

`rrcc` checks that an RPM repository is consistent: that all files referenced
from the metadata in the `repodata` directory exist and have the correct size
and, optionally, the correct checksum. A typical use case is mirroring a
remote RPM repository and wanting to automate checks that tell you whether
the mirror is in a usable state.

Key features:

- Checks repositories on disk or served over HTTP(S), with parallel requests,
  proxy support and client certificates (for example for the RHEL CDN).
- Optionally verifies the checksum of every package and metadata file.
- Handles mirrors made with `dnf reposync --newest-only` and modular
  repositories, as dnf 4 and dnf 5 do.
- Reports mirrors that are consistent but have stopped syncing.
- Cron friendly: can print only the repositories with problems, and nothing
  when everything is fine.

## Security

`rrcc` treats the repository, and the server it comes from, as untrusted
input. Locations in the metadata that are absolute or lead outside of the
repository (`../`, `//host/...`), or, for a repository in a directory, a
symlink that resolves outside of it, are refused and reported as problems
instead of being opened or requested (`--allow-symlinks-outside` turns this
off, for a mirror that intentionally symlinks packages in from a shared
pool). Redirects are followed, except from `https://` to `http://` and to
anything other than `http(s)://`, and a `--client-cert` is only ever sent to
the host given on the command line, not to one reached through a redirect.
Downloads, and directory listing pages, are bounded by the sizes in the
metadata or a fixed limit, loops and oversized trees in directory listings
are detected, a `<!DOCTYPE` in metadata XML is refused (RPM metadata never
has one; this blocks entity expansion attacks), proxy passwords are never
printed, and names from the repository are printed with control and
Unicode bidi/format characters escaped.

`rrcc` checks that a repository is *consistent*, not that it is *authentic*.
It does not verify GPG signatures of `repomd.xml` or of the packages, and it
accepts the checksum algorithms that the metadata uses, including MD5 and
SHA-1. Use `--insecure` only for servers that you trust in some other way.

## Install

You can install `rrcc` on your system with `make install`. By default it
installs under `~/.local`, but you can override that with, for example:

```
PREFIX=/usr/local make install
PREFIX=/usr make install
```

When installed with `make install`, the program is named just `rrcc`, without
the `.py` extension.

## Usage

Run with `--help` to learn how to use `rrcc`:

```
rrcc --help
```

or use the man page:

```
man rrcc
```

(from a checkout of this repository, run `./rrcc.py --help` instead).

## Supported platforms

RPM repositories are mostly used for RHEL-based distributions, so `rrcc` has
only been tested on RHEL/Rocky 8 and newer and Fedora 43 and newer.

## Requirements

`rrcc` runs on Python 3.6 or newer using only the standard library, apart from
two optional dependencies that depend on the metadata format the repository
uses:

- `.zst` metadata needs the `python3-zstandard` package (not needed on Python 3.14+).
- `.zck` metadata needs the `zchunk` utility (all Python versions).

Install them with:

### RHEL, Rocky Linux and AlmaLinux 8+

```
sudo dnf install epel-release
sudo dnf install python3-zstandard zchunk
```

### Fedora 43+

```
sudo dnf install zchunk
```

## Tests

The test suite in `tests/` uses only the Python standard library, like
`rrcc` itself. Run it with:

```
make test
```

It builds small repositories in temporary directories and serves them with
HTTP, HTTPS and proxy servers inside the test process on `127.0.0.1`, so it
needs no network access. Choose the Python version with, for example,
`make test PYTHON=python3.12`, and get a line per test with `TESTFLAGS=-v`.

Tests that need an optional tool or module are skipped when it isn't
installed: `zck`/`unzck` (zchunk metadata), `python3-zstandard` (zst metadata),
`python3-rpm` (version comparison against rpm itself) and
`python3-libmodulemd` (module metadata).

`make test-all` also runs the slow tests, which compare the package selection
of `--newest-only` with `dnf reposync --newest-only` on random repositories and
need `dnf` with its reposync plugin (`dnf-plugins-core` for dnf 4,
`dnf5-plugins` for dnf 5). With dnf 5 the comparison is made with
`--ignore-modules`.

The test certificates in `tests/support/pki/` are regenerated with
`tests/support/make-pki.sh`.

### Continuous integration

GitHub Actions runs `make check`, `make test-all` and an install test on
Rocky Linux 8, 9 and 10 and Fedora 43 and 44, with all optional dependencies
installed, on every push and pull request and once a week. The steps are in
`ci/`, and the same CI can be run locally in podman containers with:

```
make ci-local
make ci-local CI_IMAGES="rockylinux:8 fedora:44"
```

The output of each container is kept in `ci/logs/`.
