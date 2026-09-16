#!/usr/bin/env bash
# Remove what install-linux.sh installed. Workspaces are never touched.
set -euo pipefail
PREFIX="${PREFIX:-$HOME/.local}"
rm -f "$PREFIX/bin/vanilla_extract"
rm -rf "$PREFIX/lib/vanilla_extract"
echo "removed vanilla_extract from $PREFIX (workspaces and data left alone)"
