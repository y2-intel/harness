#!/bin/sh

set -eu

repository="${Y2_RELEASE_REPOSITORY:-y2-intel/harness}"
default_install_dir="${HOME}/.y2/bin"
install_dir="${Y2_INSTALL_DIR:-${default_install_dir}}"
requested_version="latest"
modify_path="true"
version_seen="false"

usage() {
    echo "Usage: install.sh [vX.Y.Z] [--no-modify-path]"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        --no-modify-path)
            modify_path="false"
            ;;
        --*)
            echo "unknown y2 installer option: $1" >&2
            usage >&2
            exit 1
            ;;
        *)
            if [ "$version_seen" = "true" ]; then
                echo "y2 installer accepts only one release version" >&2
                exit 1
            fi
            requested_version="$1"
            version_seen="true"
            ;;
    esac
    shift
done

need_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "y2 installer requires $1" >&2
        exit 1
    fi
}

need_command curl
need_command grep
need_command install
need_command mktemp
need_command tar
need_command uname

shell_profile() {
    shell_path="${SHELL:-}"
    shell_name="${shell_path##*/}"
    case "$shell_name" in
        zsh) echo "${HOME}/.zshrc" ;;
        bash)
            if [ -f "${HOME}/.bash_profile" ] && [ ! -f "${HOME}/.bashrc" ]; then
                echo "${HOME}/.bash_profile"
            else
                echo "${HOME}/.bashrc"
            fi
            ;;
        fish) echo "${HOME}/.config/fish/config.fish" ;;
        *) echo "${HOME}/.profile" ;;
    esac
}

persist_default_path() {
    [ "$modify_path" = "true" ] || return 0
    [ "$install_dir" = "$default_install_dir" ] || return 0

    profile="$(shell_profile)"
    shell_path="${SHELL:-}"
    case "${shell_path##*/}" in
        fish) profile_line="fish_add_path \"\$HOME/.y2/bin\"" ;;
        *) profile_line="export PATH=\"\$HOME/.y2/bin:\$PATH\"" ;;
    esac

    if [ -f "$profile" ] && grep -F "$profile_line" "$profile" >/dev/null 2>&1; then
        echo "y2 PATH is already configured in ${profile}"
        return 0
    fi

    profile_dir="${profile%/*}"
    mkdir -p "$profile_dir"
    {
        echo ""
        echo "# Added by the y2 installer"
        echo "$profile_line"
    } >> "$profile"
    echo "Added y2 to PATH in ${profile}"
}

is_release_version() {
    case "$1" in
        v*) release_numbers="${1#v}" ;;
        *) return 1 ;;
    esac

    release_major="${release_numbers%%.*}"
    release_remainder="${release_numbers#*.}"
    [ "$release_remainder" != "$release_numbers" ] || return 1
    release_minor="${release_remainder%%.*}"
    release_patch="${release_remainder#*.}"
    [ "$release_patch" != "$release_remainder" ] || return 1
    case "$release_patch" in *.*) return 1 ;; esac

    for release_component in "$release_major" "$release_minor" "$release_patch"; do
        case "$release_component" in
            ""|*[!0-9]*) return 1 ;;
            0) ;;
            0*) return 1 ;;
        esac
    done
}

case "$(uname -s)" in
    Darwin) platform="macos" ;;
    Linux) platform="linux" ;;
    *)
        echo "y2 does not publish a binary for $(uname -s)" >&2
        exit 1
        ;;
esac

case "$(uname -m)" in
    x86_64|amd64) architecture="x86_64" ;;
    arm64|aarch64) architecture="aarch64" ;;
    *)
        echo "y2 does not publish a binary for $(uname -m)" >&2
        exit 1
        ;;
esac

if [ "$requested_version" = "latest" ]; then
    release_url="https://github.com/${repository}/releases/latest"
    effective_url="$(curl -fsSL -o /dev/null -w '%{url_effective}' "$release_url")"
    case "$effective_url" in
        */releases/tag/v*) version="${effective_url##*/}" ;;
        *)
            echo "y2 has no published release at ${repository}" >&2
            exit 1
            ;;
    esac
else
    version="$requested_version"
fi

if ! is_release_version "$version"; then
    echo "invalid y2 release version: $version" >&2
    exit 1
fi

archive="y2-${platform}-${architecture}.tar.gz"
release_base="${Y2_RELEASE_BASE_URL:-https://github.com/${repository}/releases/download/${version}}"
temporary_dir="$(mktemp -d "${TMPDIR:-/tmp}/y2-install.XXXXXX")"

cleanup() {
    rm -rf "$temporary_dir"
}
trap cleanup EXIT HUP INT TERM

curl -fsSL "${release_base}/${archive}" -o "${temporary_dir}/${archive}"
curl -fsSL "${release_base}/${archive}.sha256" -o "${temporary_dir}/${archive}.sha256"

if command -v sha256sum >/dev/null 2>&1; then
    (cd "$temporary_dir" && sha256sum -c "${archive}.sha256")
elif command -v shasum >/dev/null 2>&1; then
    expected="$(awk '{print $1}' "${temporary_dir}/${archive}.sha256")"
    actual="$(shasum -a 256 "${temporary_dir}/${archive}" | awk '{print $1}')"
    if [ "$expected" != "$actual" ]; then
        echo "y2 archive checksum verification failed" >&2
        exit 1
    fi
else
    echo "y2 installer requires sha256sum or shasum" >&2
    exit 1
fi

tar -xzf "${temporary_dir}/${archive}" -C "$temporary_dir"
if [ ! -x "${temporary_dir}/y2" ]; then
    echo "y2 release archive did not contain an executable" >&2
    exit 1
fi

mkdir -p "$install_dir"
install "${temporary_dir}/y2" "${install_dir}/y2"

echo "Installed y2 ${version} to ${install_dir}/y2"
persist_default_path
case ":${PATH}:" in
    *":${install_dir}:"*) echo "y2 is ready. Run: y2" ;;
    *)
        if [ "$modify_path" = "true" ] && [ "$install_dir" = "$default_install_dir" ]; then
            echo "Open a new terminal to run y2."
        else
            echo "Run y2 now with: ${install_dir}/y2"
        fi
        ;;
esac
