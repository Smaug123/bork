#!/usr/bin/env python3
"""Coding harness: reads specs and codebase, asks an LLM to bring codebase into compliance."""

import json
import os
import subprocess
import sys
from pathlib import Path, PurePath

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
    for raw in gi.read_text(encoding="utf-8").splitlines():
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


def _run_git(root: Path, args: list[str]) -> tuple[int, str, str]:
    """Run a git command in root and return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            text=True,
            capture_output=True,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", "git not found"


def _run_git_z(root: Path, args: list[str]) -> tuple[int, list[str], str]:
    """Run a git command returning NUL-separated paths."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0:
            return proc.returncode, [], proc.stderr.decode("utf-8", errors="replace")
        raw = proc.stdout.decode("utf-8", errors="replace")
        items = [p for p in raw.split("\0") if p]
        return 0, items, ""
    except FileNotFoundError:
        return 127, [], "git not found"


def _in_git_repo(root: Path) -> bool:
    rc, out, _ = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    return rc == 0 and out.strip() == "true"


def _git_ref_exists(root: Path, ref: str) -> bool:
    rc, _, _ = _run_git(root, ["rev-parse", "--verify", "--quiet", ref])
    return rc == 0


def _choose_main_ref(root: Path) -> str | None:
    for ref in ("main", "origin/main", "refs/remotes/origin/main"):
        if _git_ref_exists(root, ref):
            return ref
    return None


def _collect_files_via_git(root: Path) -> dict[str, str] | None:
    """Collect files using git, respecting .gitignore via --exclude-standard.

    Returns None if git isn't available or we aren't in a git repo.
    """
    if not _in_git_repo(root):
        return None

    # Tracked files (cached) + untracked (others) excluding ignored.
    rc1, tracked, _ = _run_git_z(root, ["ls-files", "-z", "--cached"])
    rc2, untracked, _ = _run_git_z(root, ["ls-files", "-z", "--others", "--exclude-standard"])
    if rc1 != 0 and rc2 != 0:
        return None

    paths = sorted(set(tracked) | set(untracked))

    files: dict[str, str] = {}
    for rel in paths:
        p = Path(rel)
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        abs_path = root / p
        if not abs_path.is_file():
            continue
        try:
            files[rel] = abs_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError):
            # Best-effort: omit binary/undecodable files.
            pass
    return files


def collect_files(root: Path) -> dict[str, str]:
    """Collect all text files in the repo, omitting `.gitignore`'d files."""
    git_files = _collect_files_via_git(root)
    if git_files is not None:
        return git_files

    # Fallback (no git): approximate using top-level .gitignore patterns.
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
            files[rel] = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError, OSError):
            pass
    return files


def compute_specs_status(root: Path) -> tuple[str | None, str, list[str]]:
    """Compute a diff of specs/ vs the main branch (best-effort) and list new untracked spec files.

    Returns:
      (baseline_ref, unified_diff_text, untracked_spec_files)

    Notes:
      - unified_diff_text is empty if there are no changes or diff couldn't be computed.
      - untracked_spec_files are paths relative to repo root.
    """
    if not _in_git_repo(root):
        return None, "", []

    baseline = _choose_main_ref(root)

    # New unstaged (untracked) spec files.
    rc, out, _ = _run_git(root, ["ls-files", "--others", "--exclude-standard", "--", "specs/"])
    untracked = [ln.strip() for ln in out.splitlines() if ln.strip()] if rc == 0 else []

    diff_text = ""
    if baseline is not None:
        rc, out, _ = _run_git(root, ["diff", "--no-color", baseline, "--", "specs/"])
        if rc == 0:
            diff_text = out

    return baseline, diff_text, untracked


def build_prompt(
    files: dict[str, str],
    *,
    specs_baseline_ref: str | None = None,
    specs_diff_text: str = "",
    newly_added_specs: set[str] | None = None,
) -> str:
    """Build the concatenated codebase + specs prompt."""
    newly_added_specs = newly_added_specs or set()

    # Optional: include a specs/ diff vs main branch, if present.
    extra_sections: list[str] = []
    if specs_diff_text.strip() or newly_added_specs:
        if specs_baseline_ref is None:
            header = "--- SPECS STATUS (baseline ref not found; unable to diff vs main) ---"
        else:
            header = f"--- SPECS DIFF VS {specs_baseline_ref} ---"

        lines: list[str] = [header]
        if specs_baseline_ref is not None:
            if specs_diff_text.strip():
                lines.append(specs_diff_text.rstrip("\n"))
            else:
                lines.append("(no textual diff output)")

        # Per spec: new unstaged spec files are not duplicated in the input; their
        # filepath is indicated as "newly added" in the file boundary markers below.
        if newly_added_specs:
            lines.append(
                "Note: Newly added (untracked) spec files are marked as '(newly added)' in their file headers below."
            )

        lines.append("--- END SPECS STATUS ---")
        extra_sections.append("\n".join(lines))

    parts: list[str] = []
    for path, content in sorted(files.items()):
        if path in newly_added_specs:
            parts.append(
                f"--- FILE (newly added): {path} ---\n{content}\n--- END FILE: {path} ---"
            )
        else:
            parts.append(f"--- FILE: {path} ---\n{content}\n--- END FILE: {path} ---")

    joined = "\n\n".join([*extra_sections, *parts])

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


def _reject_path_traversal(rel: str) -> None:
    """Reject paths that express traversal or are otherwise unsafe.

    Spec requirement: ensure there are no file path traversals expressed by keys.
    """
    if not isinstance(rel, str):
        raise ValueError("path must be a string")
    if rel == "" or rel.strip() == "":
        raise ValueError("empty path rejected")
    if "\x00" in rel:
        raise ValueError("NUL byte in path rejected")

    # Disallow backslashes to avoid alternate separators on Windows and ambiguity.
    if "\\" in rel:
        raise ValueError(f"backslash in path rejected: {rel}")

    p = PurePath(rel)

    # Absolute paths or drive-qualified paths are rejected.
    if p.is_absolute() or getattr(p, "drive", ""):
        raise ValueError(f"absolute/drive path rejected: {rel}")

    # Reject traversal and no-op segments.
    if any(part in {"..", "."} for part in p.parts):
        raise ValueError(f"path traversal segment rejected: {rel}")


def _assert_no_symlink_ancestors(root: Path, rel: str) -> None:
    """Ensure no existing ancestor in rel is a symlink, and the final target isn't either."""
    current = root
    parts = Path(rel).parts

    for part in parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError(f"symlink in path rejected: {current}")

    target = root / rel
    if target.exists() and target.is_symlink():
        raise ValueError(f"symlink target rejected: {rel}")


def validate_path(rel: str, root: Path) -> Path:
    """Resolve a relative path under root, rejecting traversals and symlinks."""
    _reject_path_traversal(rel)

    resolved = (root / rel).resolve()
    root_resolved = root.resolve()

    # Ensure the resolved path stays within root.
    try:
        resolved.relative_to(root_resolved)
    except ValueError as e:
        raise ValueError(f"path traversal rejected: {rel}") from e

    _assert_no_symlink_ancestors(root, rel)
    return resolved


def _safe_write_text(target: Path, content: str) -> None:
    """Write text to target while resisting symlink tricks (best-effort)."""
    if not isinstance(content, str):
        raise ValueError(f"file contents for {target} must be a string")

    # Best-effort: refuse to follow a symlink as the final component on platforms
    # that support O_NOFOLLOW.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    fd = os.open(str(target), flags, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    finally:
        # fdopen closes fd on exit; if an exception happened before fdopen, close here.
        try:
            os.close(fd)
        except OSError:
            pass


def apply_changes(changes: dict, root: Path) -> None:
    """Apply create-or-update and delete operations."""
    for rel, content in changes.get("create-or-update", {}).items():
        parts = Path(rel).parts
        if parts and parts[0] == ".git":
            print(f"  skipping .git path: {rel}", file=sys.stderr)
            continue
        if parts and parts[0] == "specs":
            # Specs are immutable from the point of view of the LLM.
            print(f"  skipping immutable specs path: {rel}", file=sys.stderr)
            continue

        target = validate_path(rel, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-check after mkdir (best-effort against symlink races).
        _assert_no_symlink_ancestors(root, rel)
        _safe_write_text(target, content)
        print(f"  wrote: {rel}", file=sys.stderr)

    for rel in changes.get("delete", []):
        parts = Path(rel).parts
        if parts and parts[0] == ".git":
            print(f"  skipping .git path: {rel}", file=sys.stderr)
            continue
        if parts and parts[0] == "specs":
            print(f"  skipping immutable specs path: {rel}", file=sys.stderr)
            continue

        target = validate_path(rel, root)
        if target.exists():
            if target.is_dir():
                raise IsADirectoryError(f"refusing to delete directory (files only): {rel}")
            target.unlink()
            print(f"  deleted: {rel}", file=sys.stderr)


def main() -> None:
    root = Path.cwd()

    specs_baseline, specs_diff_text, untracked_specs = compute_specs_status(root)
    newly_added_specs = set(untracked_specs)

    files = collect_files(root)
    prompt = build_prompt(
        files,
        specs_baseline_ref=specs_baseline,
        specs_diff_text=specs_diff_text,
        newly_added_specs=newly_added_specs,
    )

    print(f"Collected {len(files)} files, sending to LLM...", file=sys.stderr)

    # Spec: use the most advanced model (currently gpt-5.2) with high reasoning,
    # and a 1 hour timeout on requests.
    client = OpenAI(timeout=60 * 60)
    response = client.chat.completions.create(
        model="gpt-5.2",
        messages=[{"role": "user", "content": prompt}],
        reasoning_effort="high",
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content
    changes = json.loads(raw)

    print("Applying changes...", file=sys.stderr)
    apply_changes(changes, root)
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
