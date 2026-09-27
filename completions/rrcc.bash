# bash completion for rrcc (RPM Repository Consistency Checker)

_rrcc()
{
    local cur="${COMP_WORDS[COMP_CWORD]}"
    local prev="${COMP_WORDS[COMP_CWORD-1]}"
    local opts="-h --help --top-level --checksum --extra -n --newest-only --ignore-modules -j --jobs
                --ca-cert --client-cert --client-key -k --insecure -v --verbose --version"

    compopt -o filenames 2>/dev/null
    case $prev in
        --ca-cert)
            # a PEM file or a directory of CA certificates
            COMPREPLY=( $(compgen -f -- "$cur") )
            return 0
            ;;
        --client-cert|--client-key)
            COMPREPLY=( $(compgen -f -- "$cur") )
            return 0
            ;;
        -j|--jobs)
            compopt +o filenames 2>/dev/null
            COMPREPLY=()
            return 0
            ;;
    esac

    if [[ $cur == -* ]]; then
        COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
        return 0
    fi

    # Positional arguments are repo root directories (URLs are not completed)
    COMPREPLY=( $(compgen -d -- "$cur") )
    return 0
}

complete -F _rrcc rrcc
