#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "docker CLI not found. Install Docker (Docker Desktop on macOS, docker engine on Linux) and try again." >&2
  exit 2
fi

default_image="${BORK_DEFAULT_SANDBOX_TAG:-bork-dev-sandbox:latest}"
image="${1:-$default_image}"

if [ "$#" -gt 1 ]; then
  echo "Usage: $0 [image-tag]" >&2
  echo "(This command intentionally does not accept arbitrary extra docker run flags, to avoid accidentally granting the sandbox extra filesystem access.)" >&2
  exit 2
fi

# If the image isn't present locally, encourage building it.
if ! docker image inspect "$image" >/dev/null 2>&1; then
  echo "Docker image not found locally: $image" >&2
  echo "Build it with: nix run .#sandbox-build" >&2
  exit 2
fi

# Mount only the working tree (the Git repo) from the host.
# Pass through common API-related environment variables.
# Enter the flake's devShell inside the container.
docker run --rm -it \
  --mount "type=bind,source=$PWD,target=/workspace" \
  -w /workspace \
  -e OPENAI_API_KEY \
  -e OPENAI_BASE_URL \
  -e OPENAI_ORG_ID \
  -e OPENAI_PROJECT \
  -e HTTP_PROXY \
  -e HTTPS_PROXY \
  -e NO_PROXY \
  -e ANTHROPIC_API_KEY \
  "$image" \
  nix develop
