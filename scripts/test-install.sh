#!/bin/sh

set -eu

repo_root="$(
    unset CDPATH
    cd -- "$(dirname "$0")/.."
    pwd
)"
test_root="$(mktemp -d "${TMPDIR:-/tmp}/y2-installer-test.XXXXXX")"

cleanup() {
    rm -rf "$test_root"
}
trap cleanup EXIT HUP INT TERM

case "$(uname -s)" in
    Darwin) platform="macos" ;;
    Linux) platform="linux" ;;
    *) echo "unsupported installer test platform" >&2; exit 1 ;;
esac

case "$(uname -m)" in
    x86_64|amd64) architecture="x86_64" ;;
    arm64|aarch64) architecture="aarch64" ;;
    *) echo "unsupported installer test architecture" >&2; exit 1 ;;
esac

archive="y2-${platform}-${architecture}.tar.gz"
fixture_dir="${test_root}/fixture"
release_dir="${test_root}/release"
mkdir -p "$fixture_dir" "$release_dir"
printf '#!/bin/sh\necho 0.0.7\n' > "${fixture_dir}/y2"
chmod +x "${fixture_dir}/y2"
tar -czf "${release_dir}/${archive}" -C "$fixture_dir" y2

if command -v sha256sum >/dev/null 2>&1; then
    checksum="$(sha256sum "${release_dir}/${archive}" | awk '{print $1}')"
else
    checksum="$(shasum -a 256 "${release_dir}/${archive}" | awk '{print $1}')"
fi
printf '%s  %s\n' "$checksum" "$archive" > "${release_dir}/${archive}.sha256"

profile_home="${test_root}/profile-home"
mkdir -p "$profile_home"
PATH="$PATH" \
HOME="$profile_home" \
SHELL="/bin/zsh" \
Y2_RELEASE_BASE_URL="file://${release_dir}" \
sh "${repo_root}/scripts/install.sh" v0.0.7 > "${test_root}/first.out"

profile_line="export PATH=\"\$HOME/.y2/bin:\$PATH\""
grep -Fx "$profile_line" "${profile_home}/.zshrc" >/dev/null
grep -F "Added y2 to PATH in ${profile_home}/.zshrc" "${test_root}/first.out" >/dev/null
grep -F "Open a new terminal to run y2." "${test_root}/first.out" >/dev/null
test "$("${profile_home}/.y2/bin/y2")" = "0.0.7"

PATH="${profile_home}/.y2/bin:${PATH}" \
HOME="$profile_home" \
SHELL="/bin/zsh" \
Y2_RELEASE_BASE_URL="file://${release_dir}" \
sh "${repo_root}/scripts/install.sh" v0.0.7 > "${test_root}/second.out"

test "$(grep -Fxc "$profile_line" "${profile_home}/.zshrc")" = "1"
grep -F "y2 PATH is already configured in ${profile_home}/.zshrc" "${test_root}/second.out" >/dev/null
grep -F "y2 is ready. Run: y2" "${test_root}/second.out" >/dev/null

isolated_home="${test_root}/isolated-home"
mkdir -p "$isolated_home"
HOME="$isolated_home" \
SHELL="/bin/zsh" \
Y2_RELEASE_BASE_URL="file://${release_dir}" \
sh "${repo_root}/scripts/install.sh" v0.0.7 --no-modify-path > "${test_root}/isolated.out"

test ! -e "${isolated_home}/.zshrc"
grep -F "Run y2 now with: ${isolated_home}/.y2/bin/y2" "${test_root}/isolated.out" >/dev/null

echo "installer path setup checks passed"
