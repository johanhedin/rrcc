# RPM Repository Consistency Checker

This repository contains a Python script called `rrcc`, which stands
for "RPM Repository Consistency Checker".

The script can be run against an RPM repository on disk to check if the
repository is consistent. A repository is consistent if all files referenced
from the metadata in the `repodata` directory exist and have the correct
size and, optionally, the correct checksum.

A repository served over HTTP(S) can be checked as well by giving its URL
instead of a directory, for example `rrcc http://host/path/to/repo`. Only the
metadata and, with `--checksum`, the packages are downloaded; the presence and
size of each package is checked with a HEAD request. Connections are kept
alive and several packages are checked in parallel, by default 8; change that
with `--jobs`. A proxy is used if it is set in the `http_proxy`/`https_proxy`
environment variables, except for hosts listed in `no_proxy`.

With `--newest-only` only the latest version of each package is checked,
which is what you want for a mirror made with `dnf reposync --newest-only`.

A typical use case is mirroring a remote RPM repository and wanting to
automate checks that tell you whether the mirror is in a usable state.

## Usage

Run with `--help` to learn how to use `rrcc`:

```
rrcc --help
```

From a checkout of this repository, run `./rrcc.py --help` instead.

## Install

You can install `rrcc` on your system with `make install`. By default it
installs under `~/.local`, but you can override that with, for example:

```
PREFIX=/usr/local make install
PREFIX=/usr make install
```

When installed with `make install`, the program is named just `rrcc`, without
the `.py` extension.

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
