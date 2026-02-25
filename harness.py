#!/usr/bin/env python3
"""Coding harness: reads specs and codebase, asks an LLM to bring codebase into compliance."""

import json
import os
import sys
from pathlib import Path

from openai import OpenAI

SKIP_DIRS = {".git", ".direnv", "__pycache__", ".claude"}


def _load_gitignore_patterns(root: Path) -> list[str]:
    """Load top-level .gitignore patterns if present.

    This is a minimal parser sufficient for typical ignore use in this repo.
    """
    gi = root / ".gitignore"
    if not gi.exists():
        return []

    patterns: list[str] = []
    for raw in gi.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # ignore negation for now; spec only requires omitting gitignored files
        if line.startswith("!"):
            continue
        patterns.append(line)
    return patterns


def _is_ignored(rel: str, patterns: list[str]) -> bool:
    """Return True if rel path matches any .gitignore-like pattern.

    Supports:
      - exact directory ignores like ".venv/" and ".direnv/"
      - exact file ignores
      - simple globbing via fnmatch
    """
    import fnmatch

    # normalize to forward slashes
    rel = rel.replace(os.sep, "/")

    parts = rel.split("/")
    for pat in patterns:
        pat = pat.strip()
        if not pat:
            continue
        # normalize pattern to forward slashes
        pat = pat.replace(os.sep, "/")

        # directory pattern like ".venv/": ignore if any path segment equals ".venv"
        if pat.endswith("/"):
            d = pat[:-1]
            if d in parts:
                return True
            # also match prefix directory
            if rel.startswith(d + "/"):
                return True
            continue

        # anchored pattern "/foo": treat as repo-root anchored
        anchored = pat.startswith("/")
        if anchored:
            p = pat[1:]
            if rel == p or rel.startswith(p + "/"):
                return True
            # allow glob anchored
            if fnmatch.fnmatch(rel, p):
                return True
            continue

        # unanchored
        if rel == pat:
            return True
        if fnmatch.fnmatch(rel, pat):
            return True
        # also match basename for patterns without slashes
        if "/" not in pat and fnmatch.fnmatch(parts[-1], pat):
            return True

    return False


def collect_files(root: Path) -> dict[str, str]:
    """Collect all text files in the repo, skipping SKIP_DIRS and .gitignore'd files."""
    files: dict[str, str] = {}
    ignore_patterns = _load_gitignore_patterns(root)

    for path in sorted(root.rglob("*")):
        rel_path = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel_path.parts):
            continue
        if not path.is_file():
            continue

        rel = str(rel_path)
        if _is_ignored(rel, ignore_patterns):
            continue

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
