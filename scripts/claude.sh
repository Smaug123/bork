#!/bin/sh
set -eu

# Provide a stable `claude` entrypoint.
# Depending on Nixpkgs packaging/version, the `claude-code` package may expose either:
#   - a `claude-code` binary, or
#   - a `claude` binary.
#
# This wrapper supports both, while avoiding recursion if the underlying binary is also named `claude`.

if command -v claude-code >/dev/null 2>&1; then
  exec claude-code "$@"
fi

# Try to find an underlying `claude` binary by removing this wrapper's directory from PATH.
self="$0"
case "$self" in
  */*) self_dir=${self%/*} ;;
  *) self_dir="." ;;
esac

old_ifs=$IFS
IFS=:
newpath=""
for p in $PATH; do
  # Drop empty segments and the directory containing this wrapper.
  [ -n "$p" ] || continue
  [ "$p" = "$self_dir" ] && continue
  if [ -z "$newpath" ]; then
    newpath="$p"
  else
    newpath="$newpath:$p"
  fi
done
IFS=$old_ifs

PATH="$newpath"
export PATH

if command -v claude >/dev/null 2>&1; then
  exec claude "$@"
fi

echo "Neither claude-code nor an underlying claude binary were found in PATH (expected the Nix devShell to provide one)." >&2
exit 127
