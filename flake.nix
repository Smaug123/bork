{
  description = "Convergent code generation system";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          config.allowUnfree = true;
        };

        defaultSandboxTag = "bork-dev-sandbox:latest";

        # Provide a `claude` command in the devshell.
        # (Nixpkgs' package is typically `claude-code`, whose binary may not be named `claude`.)
        claude = pkgs.writeShellApplication {
          name = "claude";
          text = builtins.readFile ./scripts/claude.sh;
          runtimeInputs = [ pkgs.claude-code ];
        };

        devPackages = [
          pkgs.uv
          pkgs.codex
          claude
          pkgs.python3

          # Also expose the underlying binary directly.
          pkgs.claude-code
        ];

        sandboxBuild = pkgs.writeShellApplication {
          name = "bork-sandbox-build";
          text = builtins.readFile ./scripts/bork-sandbox-build.sh;
          runtimeEnv = {
            BORK_DEFAULT_SANDBOX_TAG = defaultSandboxTag;
          };
        };

        sandboxShell = pkgs.writeShellApplication {
          name = "bork-sandbox-shell";
          text = builtins.readFile ./scripts/bork-sandbox-shell.sh;
          runtimeEnv = {
            BORK_DEFAULT_SANDBOX_TAG = defaultSandboxTag;
          };
        };
      in
      {
        devShells.default = pkgs.mkShell {
          packages = devPackages;
        };

        apps.sandbox-build = flake-utils.lib.mkApp { drv = sandboxBuild; };
        apps.sandbox-shell = flake-utils.lib.mkApp { drv = sandboxShell; };
      });
}
