#!/usr/bin/env python3

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

    # LLM code review of changed files.
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, script_dir)
        import llm_review
        review_findings: list[dict[str, str | int]] = llm_review.review()
        for finding in review_findings:
            if 'file' in finding:
                per_file_findings.append(finding)
            else:
                overall_findings.append(finding)
    except Exception as e:
        print(f"correctness checker: LLM review failed: {e}", file=sys.stderr)

    output = {"per_file_findings": per_file_findings, "overall_findings": overall_findings}
    print(json.dumps(output))
    sys.exit(1 if per_file_findings or overall_findings else 0)


if __name__ == "__main__":
    main()
