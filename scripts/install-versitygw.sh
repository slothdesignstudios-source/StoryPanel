#!/usr/bin/env bash
# Installs the local S3 storage server used by scripts/native-stack.sh: Versity S3 Gateway v1.11.0
# (the version docker-compose.yml uses), POSIX backend: every object is a file on disk.
# It is built from source with Go into ~/.local/bin/versitygw. Go checks every downloaded module
# against the public checksum database (sum.golang.org), so a tampered module stops the build.
#   scripts/install-versitygw.sh
set -euo pipefail
VERSION=v1.11.0
command -v go >/dev/null || { echo "Go is required: sudo apt-get install -y golang-go  (Go then fetches the Go release Versity needs by itself)" >&2; exit 1; }
mkdir -p "$HOME/.local/bin"
GOBIN="$HOME/.local/bin" GOTOOLCHAIN=auto GOFLAGS=-mod=readonly \
  go install "github.com/versity/versitygw/cmd/versitygw@$VERSION"
echo "installed $HOME/.local/bin/versitygw $VERSION (sha256 $(sha256sum "$HOME/.local/bin/versitygw" | cut -c1-16)...)"
