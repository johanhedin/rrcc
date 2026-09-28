# fish completion for rrcc (RPM Repository Consistency Checker)

# Positional arguments are repo root directories (URLs are not completed)
complete -c rrcc -f -a '(__fish_complete_directories (commandline -ct) "Repo root")'

complete -c rrcc -s h -d 'Show a short help and exit'
complete -c rrcc -l help -d 'Show the full help and exit'
complete -c rrcc -l version -d 'Print the version and exit'
complete -c rrcc -s t -l top-level -d 'Treat each path as a parent dir, auto-discover repos under it'
complete -c rrcc -s c -l checksum -d 'Also verify checksums (slow)'
complete -c rrcc -s e -l extra -d 'Report on-disk RPMs not in metadata'
complete -c rrcc -s f -l follow-symlinks -d 'Follow symlinks leading outside a local repo (unsafe)'
complete -c rrcc -s m -l max-age -x -d 'Report repos whose repomd.xml is older than DAYS days'
complete -c rrcc -s n -l newest-only -d 'Only check the latest version of each package'
complete -c rrcc -s i -l ignore-modules -d 'With --newest-only, ignore module metadata like dnf 5'
complete -c rrcc -s j -l jobs -x -d 'Number of packages to check in parallel for http(s) repos'
complete -c rrcc -l ca-cert -r -F -d 'Trust only the CA certificate(s) in this PEM file or directory'
complete -c rrcc -l client-cert -r -F -d 'Authenticate with this client certificate (PEM)'
complete -c rrcc -l client-key -r -F -d 'Private key for --client-cert (PEM)'
complete -c rrcc -s k -l insecure -d "Don't verify the server certificate"
complete -c rrcc -l no-strict-x509 -d "Accept certificates that don't follow RFC 5280 strictly"
complete -c rrcc -s q -l quiet -d 'Print only repos with problems'
complete -c rrcc -s v -l verbose -d 'Print a line for every package checked'
complete -c rrcc -l no-progress -d "Don't show the progress line on the terminal"
