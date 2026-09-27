#compdef rrcc
# zsh completion for rrcc (RPM Repository Consistency Checker)

_arguments -s -S \
    '(- *)'{-h,--help}'[show help and exit]' \
    '(- *)--version[print the version and exit]' \
    '--top-level[treat each path as a parent dir, auto-discover repos under it]' \
    '--checksum[also verify checksums (slow)]' \
    '--extra[report on-disk RPMs not in metadata]' \
    '--max-age=[report repos whose repomd.xml is older than DAYS days]:days' \
    '(-n --newest-only)'{-n,--newest-only}'[only check the latest version of each package]' \
    '--ignore-modules[with --newest-only, ignore module metadata like dnf 5]' \
    '(-j --jobs)'{-j+,--jobs=}'[number of packages to check in parallel for http(s) repos]:number of jobs' \
    '--ca-cert=[trust only the CA certificate(s) in this PEM file or directory]:CA certificate file or directory:_files' \
    '--client-cert=[authenticate with this client certificate (PEM)]:client certificate:_files' \
    '--client-key=[private key for --client-cert (PEM)]:client key:_files' \
    '(-k --insecure)'{-k,--insecure}"[don't verify the server certificate]" \
    "--no-strict-x509[accept certificates that don't follow RFC 5280 strictly]" \
    '(-v --verbose)'{-v,--verbose}'[print a line for every package checked]' \
    '*:repo root directory or URL:_files -/'
