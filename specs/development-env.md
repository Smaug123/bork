---
kind: spec
id: core/development-env
description: Defines the dev environment in which development of the Bork system itself takes place.
---

The project is developed in a Git repo.

There is a Nix devshell in a flake which contains `uv`, `claude`, and `codex`.

The project has `direnv` integration with an `.envrc`.

The Nix flake additionally specifies how to build a Docker-based sandbox which contains the dev env; this is so that a user can develop Bork without fear of the inner correctness-checking loop having side effects on the rest of the computer, by running Bork inside the container.
It is expected that the container can be built on either a macOS or Linux machine.

Shell scripts are not inlined 
