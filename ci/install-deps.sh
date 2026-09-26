#!/bin/sh
# Install what the rrcc test suite needs, on a Rocky Linux or Fedora system
# (typically a fresh container, as root). Used by the GitHub Actions workflow
# and by ci/podman.sh.
#
# Required packages must install. Optional ones enable tests that are
# skipped otherwise; a missing optional package is reported, not an error,
# as a distribution release may simply not have it.

set -eu

# shellcheck source=/dev/null
. /etc/os-release
major=${VERSION_ID%%.*}

required="python3 make gzip"
optional="zchunk python3-rpm python3-libmodulemd"

case $ID in
    rocky|rhel|almalinux|centos)
        # EPEL for zchunk and python3-zstandard, which need CRB/PowerTools
        dnf -y -q install dnf-plugins-core epel-release
        if [ "$major" = 8 ]; then
            dnf config-manager --set-enabled powertools
        else
            dnf config-manager --set-enabled crb
        fi
        required="$required dnf-plugins-core"
        optional="$optional python3-zstandard"
        ;;
    fedora)
        # dnf 5 has reposync in dnf5-plugins; Python 3.14+ has zstd built in
        required="$required dnf5-plugins"
        ;;
    *)
        echo "install-deps.sh: unsupported distribution '$ID'" >&2
        exit 1
        ;;
esac

echo "Installing required packages: $required"
# shellcheck disable=SC2086 # a list of package names
dnf -y -q install $required

missing=""
for pkg in $optional; do
    if dnf -y -q install "$pkg" >/dev/null 2>&1; then
        echo "Installed optional package: $pkg"
    else
        missing="$missing $pkg"
    fi
done
if [ -n "$missing" ]; then
    echo "Optional packages not available on $PRETTY_NAME (their tests are skipped):$missing"
fi
