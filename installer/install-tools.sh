#!/usr/bin/env bash
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run as root" >&2; exit 1; }

UV_VERSION=0.11.21
NODE_VERSION=22.22.3
WP_CLI_VERSION=2.12.0
ADMINER_VERSION=5.4.2
FILEBROWSER_VERSION=2.63.14

download_verified() {
  local url=$1 sha256=$2 destination=$3 temporary
  temporary=$(mktemp)
  trap 'rm -f "$temporary"' RETURN
  curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
    --output "$temporary" "$url"
  echo "$sha256  $temporary" | sha256sum --check --status || {
    echo "Checksum verification failed for $url" >&2
    exit 1
  }
  install -m 0644 "$temporary" "$destination"
  rm -f "$temporary"
  trap - RETURN
}

architecture() {
  case $(uname -m) in
    x86_64) echo x86_64 ;;
    aarch64|arm64) echo aarch64 ;;
    *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
  esac
}

install_uv() {
  local arch asset sha archive temp
  arch=$(architecture)
  if [[ $arch == x86_64 ]]; then
    asset=uv-x86_64-unknown-linux-gnu.tar.gz
    sha=8c88519b0ef0af9801fcdee419bbb12116bd9e6b18e162ae093c932d8b264050
  else
    asset=uv-aarch64-unknown-linux-gnu.tar.gz
    sha=88e800834007cc5efd4675f166eb2a51e7e3ad19876d85fa8805a6fb5c922397
  fi
  archive=$(mktemp)
  temp=$(mktemp -d)
  download_verified "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$asset" "$sha" "$archive"
  tar -xzf "$archive" -C "$temp" --strip-components=1
  install -m 0755 "$temp/uv" "$temp/uvx" /usr/local/bin/
  rm -rf "$archive" "$temp"
}

install_node() {
  local arch node_arch sha archive prefix
  arch=$(architecture)
  if [[ $arch == x86_64 ]]; then
    node_arch=x64
    sha=2e5d13569282d016861fae7c8f935e741693c269101a5bebcf761a5376d1f99f
  else
    node_arch=arm64
    sha=1c4a9933a5e45bc88f54f70b5f91232c127ec49f1a5989d23fb85824c7adf9b7
  fi
  archive=$(mktemp)
  prefix="/usr/local/lib/nodejs/node-v$NODE_VERSION-linux-$node_arch"
  install -d -m 0755 /usr/local/lib/nodejs
  download_verified \
    "https://nodejs.org/download/release/v$NODE_VERSION/node-v$NODE_VERSION-linux-$node_arch.tar.xz" \
    "$sha" "$archive"
  rm -rf "$prefix"
  tar -xJf "$archive" -C /usr/local/lib/nodejs
  for executable in node npm npx corepack; do
    ln -sfn "$prefix/bin/$executable" "/usr/local/bin/$executable"
  done
  rm -f "$archive"
}

install_wp_cli() {
  download_verified \
    "https://github.com/wp-cli/wp-cli/releases/download/v$WP_CLI_VERSION/wp-cli-$WP_CLI_VERSION.phar" \
    ce34ddd838f7351d6759068d09793f26755463b4a4610a5a5c0a97b68220d85c \
    /usr/local/bin/wp
  chmod 0755 /usr/local/bin/wp
}

install_adminer() {
  install -d -m 0755 /var/lib/hosty/adminer
  download_verified \
    "https://github.com/vrana/adminer/releases/download/v$ADMINER_VERSION/adminer-$ADMINER_VERSION-en.php" \
    f8b1cdc676d72e88d2d470dd05f2dcb7212bf6cdcf78f1eadb7fc292f4cefd39 \
    /var/lib/hosty/adminer/adminer.php
}

install_filebrowser() {
  local arch asset sha archive temp
  arch=$(architecture)
  asset=linux-amd64-filebrowser.tar.gz
  sha=bf50a3a129822f9541e6dd0f7a5d3fb482a686f2462782757fd29f7b7a15cf08
  if [[ $arch == aarch64 ]]; then
    asset=linux-arm64-filebrowser.tar.gz
    sha=16772daa9ea9d5c41d7731cdf407cbbede98a767a13e68ab09a7a1564bb9d3ad
  fi
  archive=$(mktemp)
  temp=$(mktemp -d)
  download_verified \
    "https://github.com/filebrowser/filebrowser/releases/download/v$FILEBROWSER_VERSION/$asset" \
    "$sha" "$archive"
  tar -xzf "$archive" -C "$temp"
  install -m 0755 "$temp/filebrowser" /usr/local/bin/filebrowser
  rm -rf "$archive" "$temp"
}

case ${1:-all} in
  uv) install_uv ;;
  node) install_node ;;
  wp-cli) install_wp_cli ;;
  adminer) install_adminer ;;
  filebrowser) install_filebrowser ;;
  all)
    install_uv
    install_node
    install_wp_cli
    install_adminer
    install_filebrowser
    ;;
  *) echo "Unknown tool: $1" >&2; exit 1 ;;
esac
