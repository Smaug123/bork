#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "docker CLI not found. Install Docker (Docker Desktop on macOS, docker engine on Linux) and try again." >&2
  exit 2
fi

default_image="${BORK_DEFAULT_SANDBOX_TAG:-bork-dev-sandbox:latest}"
image="${1:-$default_image}"

# If an explicit image arg was provided, shift it off so callers can pass extra docker run flags.
if [ "${1-}" != "" ]; then
  shift || true
fi

# If the image isn't present locally, encourage building it.
if ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "Docker image not found locally: $image" >&2
  echo "Build it with: nix run .#sandbox-build" >&2
  exit 2
fi

# Mount the working tree so edits are reflected on the host.
# Enter the flake's devShell inside the container.
docker run --rm -it \
  -v "$PWD":/workspace \
  -w /workspace \
  "$@" \
  "$image" \
  nix develop
