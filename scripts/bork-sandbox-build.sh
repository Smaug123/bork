#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "docker CLI not found. Install Docker (Docker Desktop on macOS, docker engine on Linux) and try again." >&2
  exit 2
fi

if [ ! -f "./flake.nix" ] || [ ! -f "./docker/Dockerfile" ]; then
  echo "Expected ./flake.nix and ./docker/Dockerfile. Run from the repository root." >&2
  exit 2
fi

default_tag="${BORK_DEFAULT_SANDBOX_TAG:-bork-dev-sandbox:latest}"
tag="${1:-$default_tag}"

docker build -f docker/Dockerfile -t "$tag" .

echo "Built sandbox image: $tag" >&2
echo "Next: nix run .#sandbox-shell -- $tag" >&2
