#!/bin/sh
# Run all checks of the rrcc CI from the top of a source tree:
# the version/date check, the full test suite and an install test.
# Used by the GitHub Actions workflow and by ci/podman.sh.

set -eu
cd "$(dirname "$0")/.."

section() {
    printf '\n==> %s\n' "$1"
}

section "System"
# shellcheck source=/dev/null
. /etc/os-release
echo "$PRETTY_NAME, $(python3 --version), $(dnf --version 2>/dev/null | head -n 1)"

section "make check"
make check

section "make test-all"
make test-all TESTFLAGS=-v

section "make install"
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
make install PREFIX=/usr DESTDIR="$stage"
for f in usr/bin/rrcc \
         usr/share/man/man1/rrcc.1.gz \
         usr/share/bash-completion/completions/rrcc \
         usr/share/doc/rrcc/README.md \
         usr/share/doc/rrcc/ChangeLog; do
    if [ ! -f "$stage/$f" ]; then
        echo "ERROR: make install did not install /$f" >&2
        exit 1
    fi
done
"$stage/usr/bin/rrcc" --version
"$stage/usr/bin/rrcc" --help >/dev/null
gzip -t "$stage/usr/share/man/man1/rrcc.1.gz"
echo "Installed files OK"

section "All checks passed"
