#!/bin/sh
set -eu

# Warm the Nix store with the dev environment so the Docker image contains the dev tools.
# Avoid inlining shell snippets in the Dockerfile; keep logic in scripts.

nix --extra-experimental-features "nix-command flakes" --accept-flake-config develop --command sh ./scripts/devshell-smoke-test.sh
