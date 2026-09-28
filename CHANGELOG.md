# Changelog

All notable changes to rrcc are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Short options `-t`, `-c`, `-e`, `-f`, `-m` and `-i` for `--top-level`,
  `--checksum`, `--extra`, `--follow-symlinks`, `--max-age` and
  `--ignore-modules`.

### Changed

- Renamed `--allow-symlinks-outside` to `--follow-symlinks`, which fits the
  help text. The old name is no longer accepted.

### Fixed

- Keep argparse's colored help on Python 3.14.

## [1.5.2] - 2026-09-28

### Added

- `SECURITY.md` with the security policy.

### Fixed

- `--top-level` now understands "fancy" directory listings: links to
  `dir/index.html` (e.g. static S3 bucket indexes), links whose text ends in
  a slash, and extension-less links that the server redirects to the same
  URL with a trailing slash ([#13]).

## [1.5.1] - 2026-09-27

### Added

- `--allow-symlinks-outside` for a mirror that symlinks packages in on
  purpose (renamed to `--follow-symlinks` in a later release).

### Changed

- `--version` only prints the version, without the program name.

### Fixed

- Treat more failures while reading a repository (a malformed HTTP response,
  a truncated body) as a problem with that repository instead of crashing
  rrcc, so the other repositories are still checked.

### Security

- Refuse a symlink, in a repository given as a directory, that resolves
  outside of it, like any other location outside of the repository.
- Refuse a `<!DOCTYPE` in metadata XML, which RPM metadata never has, closing
  off entity expansion ("billion laughs") on older Python/expat versions.
- Bound directory listing pages and the bodies drained from a redirect or
  error response, and cap the number of directories a listing can follow, so
  a hostile server can't exhaust memory or hang rrcc.
- Don't send `--client-cert` to a host reached through a redirect, only to
  the host given on the command line.
- Escape Unicode bidi and format characters in names from the repository,
  like control characters are already escaped.

## [1.5.0] - 2026-09-27

### Added

- zsh and fish completions, installed by `make install` next to the bash
  completion.
- `--max-age` to report repositories whose metadata is older than a number of
  days.
- `--no-strict-x509` to accept certificates that don't follow RFC 5280
  strictly, e.g. from a home-made CA, which Python 3.13+ rejects by default.
- `--quiet` to print only the repositories with problems, and nothing when all
  are consistent.
- A progress line with a bar, speed and time left on stderr when it is a
  terminal, and `--no-progress` to turn it off.
- `--checksum` warns about packages without a checksum.

### Changed

- The bash completion has moved to `completions/rrcc.bash`.
- `-h` prints a short help and `--help` the full help, and each option is
  listed only once in them.
- The TLS context is always created by rrcc, and a warning is printed when
  `--insecure` is used.

### Fixed

- Don't stop on a package without size in `primary.xml`.
- CodeQL findings in the test suite.

### Security

- Refuse locations in the metadata that are absolute or lead outside of the
  repository, both for directories and URLs.
- Refuse redirects from `https://` to `http://` and to other schemes.
- Limit what is downloaded, or read from a compressed file, to the sizes in
  the metadata, and stop following loops in directory listings.
- Don't print the password of a proxy URL in error messages.
- Print control characters in names from the repository as `\xNN`.

## [1.4.0] - 2026-09-26

### Added

- A test suite, run with `make test` and `make test-all`.
- `--ignore-modules` to use the `--newest-only` rules of dnf 5, which ignores
  modules, for mirrors made with dnf 5's reposync.
- GitHub CI workflow.
- Local CI in Podman containers, run with `make ci-local`.

### Changed

- Reuse the worker threads for all checks of a remote repository.

## [1.3.0] - 2026-09-26

### Added

- TLS support for `https://` repositories.
- Honor the `http_proxy`, `https_proxy` and `no_proxy` environment variables.

### Changed

- Check every metadata file listed in `repomd.xml`, not just primary.

## [1.2.1] - 2026-09-26

### Added

- `--newest-only` (`-n`) to only check the latest packages.

## [1.2.0] - 2026-09-26

### Added

- Support for checking remote repositories over HTTP(S).

## [1.1.1] - 2026-09-26

### Added

- Bash completion.
- Man page.

## [1.1.0] - 2026-09-26

### Added

- Support for installing on the system using `make`.

## [1.0.0] - 2026-09-26

### Added

- Initial release.

[Unreleased]: https://github.com/johanhedin/rrcc/compare/v1.5.2...HEAD
[1.5.2]: https://github.com/johanhedin/rrcc/compare/v1.5.1...v1.5.2
[1.5.1]: https://github.com/johanhedin/rrcc/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/johanhedin/rrcc/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/johanhedin/rrcc/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/johanhedin/rrcc/compare/v1.2.1...v1.3.0
[1.2.1]: https://github.com/johanhedin/rrcc/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/johanhedin/rrcc/compare/v1.1.1...v1.2.0
[1.1.1]: https://github.com/johanhedin/rrcc/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/johanhedin/rrcc/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/johanhedin/rrcc/releases/tag/v1.0.0
[#13]: https://github.com/johanhedin/rrcc/issues/13
