FROM ghcr.io/nixos/nix:2.22.1

# Enable flakes inside the container, and disable Nix sandboxing (commonly required in Docker).
ENV NIX_CONFIG="experimental-features = nix-command flakes\naccept-flake-config = true\nsandbox = false\n"

WORKDIR /workspace

# Copy the repository into the image so it can pre-build the devShell.
COPY . /workspace

# Warm the Nix store with the dev environment so the image "contains the dev env".
RUN sh ./scripts/docker-warmup.sh

# Default: open an interactive shell inside the devShell.
CMD ["nix", "develop"]
