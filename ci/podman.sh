#!/bin/sh
# Run the CI locally: the same scripts as the GitHub Actions workflow, in the
# same containers, using podman. The source tree is mounted read-only and
# copied inside each container, so nothing is changed here.
#
#   ci/podman.sh                        all images of the CI
#   ci/podman.sh rockylinux:9 fedora:44 just these (short names as below)
#
# The output of each run is kept in ci/logs/ (ignored by git).

set -u
cd "$(dirname "$0")/.." || exit 2

# Keep in sync with the matrix in .github/workflows/ci.yml
ALL="rockylinux:8 rockylinux:9 rockylinux:10 fedora:43 fedora:44"

image_ref() {
    case $1 in
        rockylinux:*) echo "docker.io/rockylinux/$1" ;;
        fedora:*)     echo "registry.fedoraproject.org/$1" ;;
        *)            echo "$1" ;;
    esac
}

if ! command -v podman >/dev/null; then
    echo "ci/podman.sh: podman is not installed" >&2
    exit 2
fi

images=${*:-$ALL}
mkdir -p ci/logs
status=0
summary=""
for image in $images; do
    log="ci/logs/$(echo "$image" | tr ':/' '--').log"
    printf '%-16s ... ' "$image"
    podman run --rm --security-opt label=disable -v "$PWD":/src:ro "$(image_ref "$image")" \
        sh -c 'cp -a /src /work && cd /work && rm -rf ci/logs man/rrcc.1 &&
               ci/install-deps.sh && ci/run-checks.sh' > "$log" 2>&1
    rc=$?
    if [ $rc -eq 0 ]; then
        result="ok"
    else
        result="FAILED (exit $rc)"
        status=1
    fi
    skipped=$(grep -c "\.\.\. skipped" "$log")
    echo "$result, $skipped skipped test(s), log in $log"
    summary="$summary$(printf '%-16s %s' "$image" "$result")\n"
done

printf '\n%b' "$summary"
exit $status
