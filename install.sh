#!/bin/sh
# Keep all executable code inside main. Only the complete download invokes it.
main() (
    set -eu
    umask 077

    fail() { printf 'Installer error: %s\n' "$*" >&2; exit 1; }
    boolean() {
        case "$1" in
            1|true|yes) printf '1\n' ;;
            0|false|no|'') printf '0\n' ;;
            *) fail 'Boolean environment settings must be 1, 0, true, false, yes or no.' ;;
        esac
    }
    normalize_version() {
        printf '%s\n' "$1" | awk '
            !/^[1-9][0-9][0-9][0-9]\.[0-9]+\.[0-9]+(\.[0-9]+)?$/ { exit 1 }
            { n = split($0, parts, "."); for (i = 1; i <= n; i++)
                printf "%s%d", (i == 1 ? "" : "."), parts[i]; print "" }
        '
    }
    fetch() {
        curl --fail --silent --show-error --location --connect-timeout 15 --max-time 300 \
            --proto '=https,file' --proto-redir '=https' --output "$2" "$1" \
            || fail 'Download failed. Check connectivity and the requested release assets.'
    }

    version=${COORDINARE_VERSION:-}
    yes=$(boolean "${COORDINARE_YES:-0}")
    boolean "${COORDINARE_NO_INIT:-0}" >/dev/null
    uninstall=$(boolean "${COORDINARE_UNINSTALL:-0}")
    purge=$(boolean "${COORDINARE_PURGE:-0}")
    config_directory=
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --version)
                [ "$#" -ge 2 ] || fail '--version needs a release tag, for example 2026.10.84.'
                version=$2; shift 2 ;;
            --yes) yes=1; shift ;;
            --no-init) shift ;; # Init is an explicit next step, including on a terminal.
            --uninstall) uninstall=1; shift ;;
            --purge) purge=1; shift ;;
            --config-dir)
                [ "$#" -ge 2 ] || fail '--config-dir needs a directory.'
                config_directory=$2; shift 2 ;;
            --help|-h)
                printf '%s\n' \
                    'Usage: sh install.sh [--version TAG] [--yes] [--no-init]' \
                    '       sh install.sh --uninstall [--purge] [--yes] [--config-dir DIR]' \
                    'Environment: COORDINARE_VERSION, COORDINARE_YES, COORDINARE_NO_INIT,' \
                    '             COORDINARE_UNINSTALL, COORDINARE_PURGE.' \
                    'Installation verifies release wheels, installs with uv, and prints: coordinare init.'
                exit 0 ;;
            *) fail 'Unknown option. Run sh install.sh --help.' ;;
        esac
    done
    [ "$purge" = 0 ] || [ "$uninstall" = 1 ] || fail '--purge requires --uninstall.'
    [ -z "$config_directory" ] || [ "$uninstall" = 1 ] || fail '--config-dir requires --uninstall.'
    if [ -n "$version" ]; then
        normalized=$(normalize_version "$version") || fail 'Invalid version. Use a numeric release tag such as 2026.10.84.'
    fi

    system=$(uname -s)
    architecture=$(uname -m)
    case "$system" in
        Linux|Darwin) ;;
        *) fail 'Supported systems are Linux and macOS. On Windows, use WSL and run this installer there.' ;;
    esac
    case "$architecture" in
        x86_64|amd64|arm64|aarch64) ;;
        *) fail 'Supported architectures are x86_64 and arm64.' ;;
    esac
    case "${HOME:-}" in
        /) fail 'HOME must be a user home directory, not /.' ;;
        /*) ;;
        *) fail 'Set HOME to an absolute user home directory.' ;;
    esac
    # Deliberately ignore global uv storage overrides so installation stays in HOME.
    UV_TOOL_DIR=$HOME/.local/share/uv/tools
    UV_TOOL_BIN_DIR=$HOME/.local/bin
    UV_PYTHON_INSTALL_DIR=$HOME/.local/share/uv/python
    UV_CACHE_DIR=$HOME/.cache/uv
    export UV_TOOL_DIR UV_TOOL_BIN_DIR UV_PYTHON_INSTALL_DIR UV_CACHE_DIR
    UV_NO_CONFIG=1
    export UV_NO_CONFIG
    uv_command=$(command -v uv || true)
    if [ -z "$uv_command" ] && [ -x "$HOME/.local/bin/uv" ]; then
        uv_command=$HOME/.local/bin/uv
    fi

    remove_workspace_volume() {
        if [ -z "$config_directory" ]; then
            case "${XDG_CONFIG_HOME:-}" in
                /*) config_directory=$XDG_CONFIG_HOME/coordinare ;;
                *) config_directory=$HOME/.config/coordinare ;;
            esac
        fi
        # Match user_config.workspace_volume_name: physical absolute path,
        # UTF-8 bytes with no newline, first 12 SHA256 hex digits.
        [ -d "$config_directory" ] || return 0
        config_directory=$(CDPATH='' cd -- "$config_directory" && pwd -P) || return 0
        if ! command -v docker >/dev/null 2>&1; then
            printf 'WARNING: Docker is unavailable; workspace volume cleanup skipped.\n' >&2
            return 0
        fi
        if command -v sha256sum >/dev/null 2>&1; then
            workspace_digest=$(printf '%s' "$config_directory" | sha256sum)
        elif command -v shasum >/dev/null 2>&1; then
            workspace_digest=$(printf '%s' "$config_directory" | shasum -a 256)
        else
            printf 'WARNING: SHA256 tool missing; workspace volume cleanup skipped.\n' >&2
            return 0
        fi
        workspace_digest=$(printf '%s\n' "$workspace_digest" | awk '{print substr($1, 1, 12)}')
        workspace_volume=coordinare-workspaces-$workspace_digest
        if ! docker volume inspect "$workspace_volume" >/dev/null 2>&1; then
            printf 'Workspace volume %s is absent or Docker could not inspect it; no volume removed.\n' "$workspace_volume" >&2
            return 0
        fi
        remove_volume=$yes
        if [ "$remove_volume" != 1 ] && [ -t 0 ]; then
            printf 'Remove Docker volume %s and all its workspaces [no]: ' "$workspace_volume" >&2
            read -r volume_answer || volume_answer=
            case "$volume_answer" in
                y|Y|yes|YES) remove_volume=1 ;;
            esac
        fi
        if [ "$remove_volume" = 1 ]; then
            if docker volume rm "$workspace_volume"; then
                printf 'Removed workspace volume %s.\n' "$workspace_volume"
            else
                printf 'WARNING: Could not remove workspace volume %s (it may be in use). Stop its containers and retry: docker volume rm %s\n' "$workspace_volume" "$workspace_volume" >&2
            fi
        else
            printf 'Preserved workspace volume %s. Rerun --uninstall --yes to remove it (include --config-dir for custom installs).\n' "$workspace_volume" >&2
        fi
    }

    if [ "$uninstall" = 1 ]; then
        remove_workspace_volume
        if [ -d "$UV_TOOL_DIR/coordinare" ]; then
            [ -n "$uv_command" ] || fail 'uv is missing. Install uv from https://astral.sh/uv/install.sh, then rerun --uninstall.'
            "$uv_command" tool uninstall coordinare || fail 'uv could not uninstall coordinare.'
            printf 'Removed coordinare and its bundled performer from %s.\n' "$UV_TOOL_DIR/coordinare"
        else
            printf 'coordinare is already uninstalled from %s.\n' "$UV_TOOL_DIR"
        fi
        if [ "$purge" = 1 ]; then
            rm -rf "$HOME/.coordinare"
            printf 'Purged %s. Custom configuration and project state paths are operator managed.\n' "$HOME/.coordinare"
        else
            printf 'Preserved configuration and state.\n'
        fi
        printf 'Kept uv and its shared Python and download caches.\n'
        exit 0
    fi

    command -v curl >/dev/null 2>&1 || fail 'curl is required. Install curl, then rerun this installer.'
    if command -v sha256sum >/dev/null 2>&1; then
        checksum=sha256sum
    elif command -v shasum >/dev/null 2>&1; then
        checksum=shasum
    else
        fail 'Checksum verification requires sha256sum or shasum.'
    fi
    temporary=$(mktemp -d "${TMPDIR:-/tmp}/coordinare.XXXXXXXX") || fail 'Cannot create a temporary directory.'
    trap 'rm -rf "$temporary"' 0
    trap 'exit 1' HUP INT TERM
    root=${COORDINARE_RELEASE_ROOT:-https://github.com/ViviDynamics/coordinare/releases}
    root=${root%/}
    case "$root" in
        https://*|file:///*) ;;
        *) fail 'COORDINARE_RELEASE_ROOT must be an HTTPS or local file URL.' ;;
    esac
    if [ -n "$version" ]; then
        source=$root/download/$version
    else
        source=$root/latest/download
    fi
    printf 'Detected %s %s. Downloading release checksums.\n' "$system" "$architecture"
    fetch "$source/SHA256SUMS" "$temporary/SHA256SUMS"
    # Never use manifest filenames as paths until the entire inventory is validated.
    awk '
        NF != 2 || length($1) != 64 || $1 ~ /[^a-fA-F0-9]/ ||
        $2 !~ /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/ || $2 == "SHA256SUMS" ||
        $0 != $1 "  " $2 || seen[$2]++ { exit 1 }
        END { if (NR == 0) exit 1 }
    ' "$temporary/SHA256SUMS" || fail 'Invalid checksum manifest: malformed, duplicate or unsafe entry.'
    if [ -z "$version" ]; then
        wheel=$(awk '$2 ~ /^coordinare-[0-9.]+-py3-none-any\.whl$/ { print $2; count++ }
            END { if (count != 1) exit 1 }' "$temporary/SHA256SUMS") \
            || fail 'Checksum manifest must contain exactly one coordinare wheel.'
        version=${wheel#coordinare-}
        version=${version%-py3-none-any.whl}
        normalized=$(normalize_version "$version") || fail 'Invalid version in checksum manifest.'
    fi
    printf 'Selected coordinare %s.\n' "$normalized"
    packages='coordinare performer coordinare_ci_detection coordinare_routing_models coordinare_service_inference'
    for package in $packages; do
        wheel=$package-$normalized-py3-none-any.whl
        expected=$(awk -v name="$wheel" '$2 == name { print tolower($1); count++ }
            END { if (count != 1) exit 1 }' "$temporary/SHA256SUMS") \
            || fail 'Checksum manifest is missing a required release wheel.'
        fetch "$source/$wheel" "$temporary/$wheel"
        if [ "$checksum" = sha256sum ]; then
            actual=$(sha256sum "$temporary/$wheel")
        else
            actual=$(shasum -a 256 "$temporary/$wheel")
        fi
        actual=${actual%% *}
        [ "$actual" = "$expected" ] || fail "Checksum mismatch for $wheel. Nothing was installed."
        printf 'Verified %s against SHA256SUMS.\n' "$wheel"
    done

    # Verify every release wheel before installing even uv itself.
    if [ -z "$uv_command" ]; then
        printf 'uv is missing. The official Astral installer will install it in %s.\n' "$HOME/.local/bin"
        if [ "$yes" != 1 ]; then
            if (exec 3<>/dev/tty) 2>/dev/null; then
                printf 'Install uv now? [y/N] ' >/dev/tty
                answer=''
                read -r answer </dev/tty || true
                case "$answer" in
                    y|Y|yes|YES) ;;
                    *) fail 'Installation cancelled. Install uv yourself or rerun with --yes.' ;;
                esac
            else
                fail 'No terminal for uv consent. Rerun with --yes or COORDINARE_YES=1, or install uv yourself.'
            fi
        fi
        fetch 'https://astral.sh/uv/install.sh' "$temporary/uv-installer.sh"
        UV_UNMANAGED_INSTALL=$HOME/.local/bin sh "$temporary/uv-installer.sh" \
            || fail 'Official uv installation failed.'
        uv_command=$HOME/.local/bin/uv
        [ -x "$uv_command" ] || fail 'Official installer did not create ~/.local/bin/uv.'
        printf 'Installed uv in %s.\n' "$HOME/.local/bin"
    fi

    # Shared wheel metadata points to GitHub URLs. Overrides force the resolver
    # to consume the local bytes we verified, including for draft-release smoke tests.
    file_root=$(printf '%s' "$temporary" | sed 's/%/%25/g; s/ /%20/g; s/#/%23/g; s/?/%3F/g')
    for package in coordinare_ci_detection coordinare_routing_models coordinare_service_inference; do
        printf '%s @ file://%s/%s-%s-py3-none-any.whl\n' "$package" "$file_root" "$package" "$normalized"
    done > "$temporary/overrides.txt"
    printf 'Installing coordinare %s and performer into %s.\n' "$normalized" "$UV_TOOL_DIR/coordinare"
    # Ignore project discovery in the caller's checkout as well as uv config files.
    cd "$temporary"
    "$uv_command" tool install --force --python 3.12 \
        --overrides overrides.txt \
        --with "file://$file_root/performer-$normalized-py3-none-any.whl" \
        --with-executables-from performer \
        "file://$file_root/coordinare-$normalized-py3-none-any.whl" \
        || fail 'uv tool installation failed. Resolve the uv error above and rerun.'
    installed=$("$UV_TOOL_BIN_DIR/coordinare" --version) || fail 'Installed command failed its version check.'
    [ "$installed" = "coordinare $normalized" ] || fail 'Installed version does not match the verified release.'
    printf 'Installed %s. Commands: %s/coordinare and %s/performer.\n' "$installed" "$UV_TOOL_BIN_DIR" "$UV_TOOL_BIN_DIR"
    printf 'Preserved existing configuration and state.\n'
    if [ "$(command -v coordinare || true)" != "$UV_TOOL_BIN_DIR/coordinare" ]; then
        printf '%s\n' 'For this shell and your shell profile, add:' "export PATH=\"\$HOME/.local/bin:\$PATH\""
    fi
    printf 'Next step: coordinare init\n'
)
main "$@"
