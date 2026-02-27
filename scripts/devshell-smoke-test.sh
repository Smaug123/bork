#!/bin/sh
set -eu

# This script is intended to be run *inside* the Nix devShell.
command -v uv >/dev/null 2>&1
command -v python3 >/dev/null 2>&1
command -v codex >/dev/null 2>&1
command -v claude >/dev/null 2>&1
