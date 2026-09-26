# RPM repository consistency checker
This repository contain a Python script called `rrcc` which is short
for "RPM repository consistency checker".

The script can be run against a RPM repository on disk to check if the
repo is consistent. The meaning of consistent is that all files referenced
from the metadata in the `repodata` directory exist with correct size and,
optionally, correct checksum.

Typical use case is when you mirror a remote RPM repository and like to
automate checks that tell you if the mirror is in a usable state.


## Usage
Run with `--help` to learn how to use `rrcc`:

```
rrcc.py --help
```


## Requirements
`rrcc` works on Python 3.9+ but needs the `zchunk` utility and the `python3-zstandard` Python
third-party package if on Python < 3.14. Install with:

### RHEL/Rocky/Alma 9+
```
sudo dnf install epel-release
sudo dnf install python3-zstandard zchunk
```

### Fedora "recent"
```
sudo dnf install zchunk
```
