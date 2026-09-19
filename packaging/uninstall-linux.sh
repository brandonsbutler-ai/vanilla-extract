#!/usr/bin/env bash
# Remove what install-linux.sh installed. Workspaces are never touched.
set -euo pipefail
PREFIX="${PREFIX:-$HOME/.local}"
rm -f "$PREFIX/bin/vanilla"
rm -rf "$PREFIX/lib/vanilla_extract"
# install-linux.sh creates these with mkdir -p. Remove them only if the
# uninstall left them empty -- anything else in them belongs to someone else.
for dir in "$PREFIX/bin" "$PREFIX/lib"; do
    if [[ -d "$dir" ]] && [[ -z "$(ls -A "$dir")" ]]; then
        rmdir "$dir"
    fi
done
echo "removed vanilla_extract from $PREFIX (workspaces and data left alone)"
