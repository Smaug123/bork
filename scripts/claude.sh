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
# When invoked as `claude` (without a slash), resolve `$0` via `command -v` so we can identify
# the real directory of this wrapper rather than treating it as ".".
self="$0"
case "$self" in
  */*)
    self_path="$self"
    ;;
  *)
    self_path="$(command -v "$self" 2>/dev/null || true)"
    if [ -z "$self_path" ]; then
      self_path="$self"
    fi
    ;;
esac

case "$self_path" in
  */*) self_dir=${self_path%/*} ;;
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
