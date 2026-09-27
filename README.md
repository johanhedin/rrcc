# RPM Repository Consistency Checker

[![CI](https://github.com/johanhedin/rrcc/actions/workflows/ci.yml/badge.svg)](https://github.com/johanhedin/rrcc/actions/workflows/ci.yml)
[![CodeQL](https://github.com/johanhedin/rrcc/actions/workflows/github-code-scanning/codeql/badge.svg)](https://github.com/johanhedin/rrcc/security/code-scanning)

This repository contains a Python script called `rrcc`, which stands
for "RPM Repository Consistency Checker".

The script can be run against an RPM repository on disk to check if the
repository is consistent. A repository is consistent if all files referenced
from the metadata in the `repodata` directory exist and have the correct
size and, optionally, the correct checksum. The metadata files listed in
`repodata/repomd.xml` are checked against their recorded size and checksum
too.

A repository served over HTTP(S) can be checked as well by giving its URL
instead of a directory, for example `rrcc http://host/path/to/repo`. Only the
metadata that rrcc reads and, with `--checksum`, the packages and the rest of
the metadata are downloaded; otherwise the presence and size of each file is
checked with a HEAD request. Connections are kept
alive and several packages are checked in parallel, by default 8; change that
with `--jobs`. A proxy is used if it is set in the `http_proxy`/`https_proxy`
environment variables, except for hosts listed in `no_proxy`. For HTTPS, the
CA certificates and a client certificate can be given with `--ca-cert`,
`--client-cert` and `--client-key`, for example to check the RHEL CDN with
an entitlement certificate.

With `--newest-only` only the latest version of each package is checked,
which is what you want for a mirror made with `dnf reposync --newest-only`.
Modular repositories are handled like dnf 4 (RHEL 8 to 10) does; add
`--ignore-modules` for a mirror made with dnf 5 (Fedora 41 and newer), which
ignores the modules.

With `--max-age DAYS` a repository whose `repomd.xml` is older than that is
reported as a problem, which catches a mirror that is consistent but has
stopped syncing.

A typical use case is mirroring a remote RPM repository and wanting to
automate checks that tell you whether the mirror is in a usable state.

## Security

`rrcc` treats the repository, and the server it comes from, as untrusted
input. Locations in the metadata that are absolute or lead outside of the
repository (`../`, `//host/...`) are refused and reported as problems instead
of being opened or requested. Redirects are followed, except from `https://`
to `http://` and to anything other than `http(s)://`. Downloads are bounded by
the sizes in the metadata, loops in directory listings are detected, proxy
passwords are never printed, and names from the repository are printed with
control characters escaped.

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
