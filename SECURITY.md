# Security Policy

## Supported Versions

Security fixes are made on the `main` branch and published in a new release.
Only the latest release receives security updates.

| Version | Supported          |
| ------- | ------------------ |
| 1.5.x   | :white_check_mark: |
| < 1.5   | :x:                |

## Reporting a Vulnerability

Please do not report security vulnerabilities through public GitHub issues.

Instead, report them privately through GitHub's
[private vulnerability reporting](https://github.com/johanhedin/rrcc/security/advisories/new)
("Report a vulnerability" under the repository's Security tab).

Please include:

- the version of `rrcc` (`rrcc --version`) and the distribution from `/etc/os-release`,
- a description of the issue and its impact,
- steps to reproduce it, for example a crafted `repodata` directory or
  server response.

`rrcc` is maintained on a best-effort basis. You can expect an
acknowledgement within resonable time, and to be kept informed while the issue is
investigated. If the report is accepted, a fix will be released and the
issue disclosed in a GitHub security advisory, with credit to you unless you
prefer otherwise. If it is declined, you will get an explanation of why.

The [Security](README.md#security) section of the README describes what
`rrcc` is designed to protect against, and what it is not.
