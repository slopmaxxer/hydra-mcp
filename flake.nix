{
  description = "hydra-mcp packaged from uv.lock";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs [ "x86_64-linux" "aarch64-linux" ];

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };

      overlay = workspace.mkPyprojectOverlay {
        sourcePreference = "wheel";
      };

      editableOverlay = workspace.mkEditablePyprojectOverlay {
        root = "$REPO_ROOT";
      };

      pythonSets = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python3;
        in
        (pkgs.callPackage pyproject-nix.build.packages {
          inherit python;
        }).overrideScope
          (
            lib.composeManyExtensions [
              pyproject-build-systems.overlays.wheel
              overlay
            ]
          )
      );

    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          pythonSet = pythonSets.${system}.overrideScope editableOverlay;
          virtualenv = pythonSet.mkVirtualEnv "hydra-mcp-dev-env" workspace.deps.all;
        in
        {
          default = pkgs.mkShell {
            packages = [
              virtualenv
              pkgs.uv
            ];
            env = {
              UV_NO_SYNC = "1";
              UV_PYTHON = pythonSet.python.interpreter;
              UV_PYTHON_DOWNLOADS = "never";
            };
            shellHook = ''
              unset PYTHONPATH
              export REPO_ROOT=$(git rev-parse --show-toplevel)
            '';
          };
        }
      );

      checks = forAllSystems (system: let
        pkgs = nixpkgs.legacyPackages.${system};
        testEnv = pythonSets.${system}.mkVirtualEnv "hydra-mcp-tests" workspace.deps.all;
      in {
        offline = pkgs.runCommand "hydra-mcp-offline-tests" {
          nativeBuildInputs = [ testEnv ];
        } ''
          export HOME=$TMPDIR
          # The sandbox points SSL_CERT_FILE at a missing file; httpx loads it when building a client.
          export SSL_CERT_FILE=${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt
          cp -r ${./.} source
          chmod -R u+w source
          cd source
          pytest
          touch $out
        '';
      });

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/hydra-mcp";
        };
      });

      packages = forAllSystems (system: {
        default = pythonSets.${system}.mkVirtualEnv "hydra-mcp-env" workspace.deps.default;
      });
    };
}
