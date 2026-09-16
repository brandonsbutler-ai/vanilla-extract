#!/usr/bin/env bash
# Remove what install-linux.sh installed. Workspaces are never touched.
set -euo pipefail
PREFIX="${PREFIX:-$HOME/.local}"
rm -f "$PREFIX/bin/puretext"
rm -rf "$PREFIX/lib/puretext"
echo "removed puretext from $PREFIX (workspaces and data left alone)"
