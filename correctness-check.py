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


def _collect_review_comments() -> tuple[list[dict[str, str | int]], list[dict[str, str | int]]]:
    """Interactively collect review comments from the user.

    Returns (per_file_findings, overall_findings).
    Skips silently if stdin is not a terminal.
    """
    per_file: list[dict[str, str | int]] = []
    overall: list[dict[str, str | int]] = []

    if not sys.stdin.isatty():
        return per_file, overall

    while True:
        print("\nReview comment (empty line to finish):", file=sys.stderr, flush=True)
        try:
            finding_text = input()
        except EOFError:
            break

        if not finding_text.strip():
            break

        print("File path (empty for overall finding):", file=sys.stderr, flush=True)
        try:
            file_path = input()
        except EOFError:
            file_path = ""

        finding: dict[str, str | int] = {"provenance": "code-review", "finding": finding_text.strip()}

        if file_path.strip():
            finding["file"] = file_path.strip()
            per_file.append(finding)
        else:
            overall.append(finding)

    return per_file, overall


def main() -> None:
    try:
        subprocess.run(["uv", "sync"], capture_output=True, check=True)
    except Exception as e:
        print(json.dumps({"per_file_findings": [], "overall_findings": []}))
        print(f"correctness checker failed to sync venv: {e}", file=sys.stderr)
        sys.exit(2)

    per_file_findings: list[dict[str, str | int]] = []
    overall_findings: list[dict[str, str | int]] = []

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

    if result.returncode != 0:
        overall_findings.append({
            "provenance": "command",
            "command": COMMAND,
            "stdout": _decode_or_placeholder(result.stdout),
            "stderr": _decode_or_placeholder(result.stderr),
            "exit-code": result.returncode,
        })

    review_per_file, review_overall = _collect_review_comments()
    per_file_findings.extend(review_per_file)
    overall_findings.extend(review_overall)

    output = {"per_file_findings": per_file_findings, "overall_findings": overall_findings}
    print(json.dumps(output))
    sys.exit(1 if per_file_findings or overall_findings else 0)


if __name__ == "__main__":
    main()
