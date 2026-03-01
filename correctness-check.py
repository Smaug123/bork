#!/usr/bin/env -S python3 -I

# Thanks Python for making this vulnerability so easy:
# If a file `json.py` is placed next to this script, then
# *that* `json.py` will be imported instead of stdlib `json`.
# The fix is to use `-I`, which ignores PYTHONPATH and removes the script's directory from `sys.path`.

import json
import os
import subprocess
import sys

NON_UTF8 = "<non-UTF8 output>"
COMMAND = "uv run --group dev pyright ."


def _decode_or_placeholder(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except (UnicodeDecodeError, AttributeError):
        return NON_UTF8


def main() -> None:
    # Ensure the venv is populated with dev dependencies (e.g. openai)
    # so Pyright can resolve imports.
    try:
        subprocess.run(["uv", "sync"], capture_output=True, check=True)
    except Exception as e:
        print(json.dumps({"per_file_findings": [], "overall_findings": []}))
        print(f"correctness checker failed to sync venv: {e}", file=sys.stderr)
        sys.exit(2)

    # Pyright reads typeCheckingMode from pyrightconfig.json.
    # Create a temporary one with strict mode if none exists.
    config_path = "pyrightconfig.json"
    created_config = not os.path.exists(config_path)
    if created_config:
        with open(config_path, "w") as f:
            json.dump({"typeCheckingMode": "strict"}, f)

    try:
        result = subprocess.run(
            COMMAND.split(),
            capture_output=True,
        )
    except Exception as e:
        print(json.dumps({"per_file_findings": [], "overall_findings": []}))
        print(f"correctness checker failed to invoke command: {e}", file=sys.stderr)
        sys.exit(2)
    finally:
        if created_config:
            try:
                os.remove(config_path)
            except OSError:
                pass

    stdout = _decode_or_placeholder(result.stdout)
    stderr = _decode_or_placeholder(result.stderr)
    exit_code = result.returncode

    if exit_code == 0:
        # Pyright found no errors.
        print(json.dumps({"per_file_findings": [], "overall_findings": []}))
        sys.exit(0)
    else:
        # Pyright found errors (exit 1) or had a fatal error (exit 2+).
        # Either way, report as a command finding.
        finding = {
            "provenance": "command",
            "command": COMMAND,
            "stdout": stdout,
            "stderr": stderr,
            "exit-code": exit_code,
        }
        print(json.dumps({"per_file_findings": [], "overall_findings": [finding]}))
        sys.exit(1)


if __name__ == "__main__":
    main()
