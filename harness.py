#!/usr/bin/env python3
"""Coding harness: reads specs and codebase, asks an LLM to bring codebase into compliance."""

import json
import os
import sys
from pathlib import Path

from openai import OpenAI

SKIP_DIRS = {".git", ".direnv", "__pycache__", ".claude"}


def collect_files(root: Path) -> dict[str, str]:
    """Collect all text files in the repo, skipping SKIP_DIRS."""
    files = {}
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        try:
            files[rel] = path.read_text()
        except (UnicodeDecodeError, PermissionError):
            pass
    return files


def build_prompt(files: dict[str, str]) -> str:
    """Build the concatenated codebase + specs prompt."""
    parts = []
    for path, content in sorted(files.items()):
        parts.append(f"--- FILE: {path} ---\n{content}\n--- END FILE: {path} ---")
    joined = "\n\n".join(parts)
    return (
        "You are a coding agent. Below is the entire contents of a repository, "
        "including specification documents in specs/*.md.\n\n"
        "Your job: determine what changes are needed to bring the codebase into "
        "compliance with the specs. Specs are immutable — do not include them in your output.\n\n"
        "Respond with ONLY a JSON object (no markdown fencing) with this exact schema:\n"
        '{"create-or-update": {"filepath": "file contents", ...}, "delete": ["filepath", ...]}\n\n'
        "If no changes are needed, return: "
        '{"create-or-update": {}, "delete": []}\n\n'
        f"{joined}"
    )


def validate_path(rel: str, root: Path) -> Path:
    """Resolve a relative path under root, rejecting traversals and symlinks."""
    resolved = (root / rel).resolve()
    if not str(resolved).startswith(str(root.resolve()) + os.sep) and resolved != root.resolve():
        raise ValueError(f"path traversal rejected: {rel}")
    # Check each ancestor for symlinks (before the file itself may be created)
    current = root
    for part in Path(rel).parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError(f"symlink in path rejected: {current}")
    # Reject if the target itself is an existing symlink
    target = root / rel
    if target.exists() and target.is_symlink():
        raise ValueError(f"symlink target rejected: {rel}")
    return resolved


def apply_changes(changes: dict, root: Path) -> None:
    """Apply create-or-update and delete operations."""
    for rel, content in changes.get("create-or-update", {}).items():
        if Path(rel).parts[0] == ".git":
            print(f"  skipping .git path: {rel}", file=sys.stderr)
            continue
        target = validate_path(rel, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        print(f"  wrote: {rel}", file=sys.stderr)

    for rel in changes.get("delete", []):
        if Path(rel).parts[0] == ".git":
            print(f"  skipping .git path: {rel}", file=sys.stderr)
            continue
        target = validate_path(rel, root)
        if target.exists():
            target.unlink()
            print(f"  deleted: {rel}", file=sys.stderr)


def main() -> None:
    root = Path.cwd()
    files = collect_files(root)
    prompt = build_prompt(files)

    print(f"Collected {len(files)} files, sending to LLM...", file=sys.stderr)

    client = OpenAI()
    response = client.chat.completions.create(
        model="gpt-5.2",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content
    changes = json.loads(raw)

    print("Applying changes...", file=sys.stderr)
    apply_changes(changes, root)
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
