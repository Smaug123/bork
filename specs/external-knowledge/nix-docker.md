kind: external-knowledge
id: external-knowledge/nix-docker
description: Some facts about the Nix Docker image available on the Docker Hub.
------

The most recent tag of the `nixos/nix` image available as of 2026-02-27 is `2.32.6`.

# Extract from https://hub.docker.com/r/nixos/nix/

Use this build to create your own customized images as follows:

```
FROM nixos/nix

RUN nix-channel --update

RUN nix-build -A pythonFull '<nixpkgs>'
```

## Limitations

By default sandboxing is turned off inside the container, even though it is enabled in new installations of nix. This can lead to differences between derivations built inside a docker container versus those built without any containerization, especially if a derivation relies on sandboxing to block sideloading of dependencies.

To enable sandboxing the container has to be started with the `--privileged` flag and `sandbox = true` set in `/etc/nix/nix.conf`.
