#!/usr/bin/env bash
# Install puretext on Linux.
#
#   ./packaging/install-linux.sh              # per-user, into ~/.local/bin
#   PREFIX=/usr/local sudo ./packaging/install-linux.sh
#
# Two modes, chosen automatically:
#   * if dist/puretext exists (built by packaging/build_standalone.py), that
#     single binary is installed and the target needs no Python at all;
#   * otherwise a small launcher is installed that runs the package from this
#     checkout with the system python3 -- which is enough, because the runtime
#     has no dependencies to install.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${PREFIX:-$HOME/.local}"
BINDIR="$PREFIX/bin"
LIBDIR="$PREFIX/lib/puretext"

mkdir -p "$BINDIR"

if [[ -f "$ROOT/dist/puretext" ]]; then
    install -m 0755 "$ROOT/dist/puretext" "$BINDIR/puretext"
    echo "installed standalone binary -> $BINDIR/puretext"
else
    command -v python3 >/dev/null 2>&1 || {
        echo "python3 not found, and no prebuilt binary in dist/." >&2
        echo "Either install python3 or run packaging/build_standalone.py first." >&2
        exit 1
    }
    rm -rf "$LIBDIR"
    mkdir -p "$LIBDIR"
    cp -r "$ROOT/puretext" "$LIBDIR/"
    cat > "$BINDIR/puretext" <<LAUNCHER
#!/usr/bin/env bash
exec python3 -c 'import sys; sys.path.insert(0, "$LIBDIR"); from puretext.__main__ import main; sys.exit(main())' "\$@"
LAUNCHER
    chmod 0755 "$BINDIR/puretext"
    echo "installed package -> $LIBDIR"
    echo "installed launcher -> $BINDIR/puretext"
fi

if ! printf '%s' ":$PATH:" | grep -q ":$BINDIR:"; then
    echo
    echo "NOTE: $BINDIR is not on your PATH. Add this to your shell profile:"
    echo "    export PATH=\"\$PATH:$BINDIR\""
fi

echo
"$BINDIR/puretext" --version
