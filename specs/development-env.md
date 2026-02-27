---
kind: spec
id: core/development-env
description: Defines the dev environment in which development of the Bork system itself takes place.
---

The project is developed in a Git repo.

There is a Nix devshell in a flake which contains `uv`, `claude`, and `codex`.

The project has `direnv` integration with an `.envrc`.

The Nix flake additionally specifies how to build a Docker-based sandbox (using a recent version of Nix) which contains the dev tools; this is so that a user can develop Bork without fear of the inner correctness-checking loop having side effects on the rest of the computer, by running Bork inside the container.
It is expected that the container can be built on either a macOS or Linux machine.
The sandbox permits network access to the OpenAI APIs, but does not permit access to the filesystem aside from the immutable Nix store and a copy of the Git repo.

Shell scripts are not inlined but are in standalone files (this is so that they can be checked mechanically with Shellcheck).
