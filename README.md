# RPM Repository Consistency Checker
This repository contains a Python script called `rrcc` which stands
for "RPM Repository Consistency Checker".

The script can be run against an RPM repository on disk to check if the
repo is consistent. Consistent means that all files referenced
from the metadata in the `repodata` directory exist and have the correct
size and, optionally, the correct checksum.

A typical use case is mirroring a remote RPM repository and wanting to
automate checks that tell you whether the mirror is in a usable state.

## Usage
Run with `--help` to learn how to use `rrcc`:

```
rrcc.py --help
```

## Requirements
`rrcc` runs on Python 3.9 or newer using only the standard library, with two
optional exceptions depending on the metadata format the repository uses:

- `.zst` metadata needs the `python3-zstandard` package (not needed on Python 3.14+).
- `.zck` metadata needs the `zchunk` utility (all Python versions).

Install with:

### RHEL, Rocky Linux and AlmaLinux 9+
```
sudo dnf install epel-release
sudo dnf install python3-zstandard zchunk
```

### Fedora >= 43
```
sudo dnf install zchunk
```
