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

    in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            pkgs.claude-code
            pkgs.codex
            pkgs.uv
            pkgs.python3
          ];
        };
      });
}
