#!/bin/sh
set -eu

# Warm the Nix store with the dev environment so the Docker image contains the dev tools.
# Avoid inlining shell snippets in the Dockerfile; keep logic in scripts.

nix develop --command sh ./scripts/devshell-smoke-test.sh
