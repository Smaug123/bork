FROM nixos/nix:2.32.6

WORKDIR /workspace

# Copy the repository into the image so it can pre-build the devShell.
COPY . /workspace

# Warm the Nix store with the dev environment so the image "contains the dev env".
RUN sh ./scripts/docker-warmup.sh

# Default: open an interactive shell inside the devShell.
CMD ["nix", "--extra-experimental-features", "nix-command flakes", "--accept-flake-config", "develop"]
