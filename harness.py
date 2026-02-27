#!/usr/bin/env python3
"""Coding harness: reads specs and codebase, asks an LLM to bring codebase into compliance."""

import difflib
import errno
import json
import os
import stat
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
            parts.append(f"--- FILE (newly added): {path} ---\n{content}\n--- END FILE: {path} ---")
        else:
            parts.append(f"--- FILE: {path} ---\n{content}\n--- END FILE: {path} ---")

    joined = "\n\n".join([*extra_sections, *parts])

    return (
        "You are a coding agent. Below is the entire contents of a repository, "
        "including specification documents in specs/*.md.\n\n"
        "Your job: determine what changes are needed to bring the codebase into "
        "compliance with the specs.\n\n"
        "Respond with ONLY a JSON object (no markdown fencing) with this exact schema:\n"
        '{"create-or-update": {"filepath": "file contents", ...}, "delete": ["filepath", ...]}\n\n'
        "If no changes are needed, return: "
        '{"create-or-update": {}, "delete": []}\n\n'
        "Notes:\n"
        "- You may propose changes to any files, including specs in specs/*.md, but spec changes are discouraged and may require human approval to apply.\n"
        "- Do not use filesystem traversal in paths (e.g., ../foo).\n\n"
        f"{joined}"
    )


def _reject_path_traversal(rel: str) -> None:
    """Reject paths that express traversal or are otherwise unsafe."""
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


def _dir_fd_capable() -> bool:
    """Return True if this platform/Python supports *at() style operations via dir_fd."""
    try:
        os.open(".", os.O_RDONLY, dir_fd=None)  # type: ignore[arg-type]
        return True
    except TypeError:
        return False
    except OSError:
        # If this errors for reasons other than TypeError, dir_fd is likely supported.
        return True


def _open_dir_no_symlink(name: str, *, dir_fd: int) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(name, flags, dir_fd=dir_fd)


def _safe_walk_dirs(root: Path, dir_parts: tuple[str, ...], *, create_missing: bool) -> tuple[int, list[int]]:
    """Open a directory chain under root without following symlinks.

    If create_missing is True, missing directories are created.

    Returns: (leaf_dir_fd, fds_to_close)
    """
    root_fd = os.open(str(root), os.O_RDONLY | (os.O_DIRECTORY if hasattr(os, "O_DIRECTORY") else 0))
    fds_to_close: list[int] = [root_fd]
    current_fd = root_fd

    for part in dir_parts:
        while True:
            try:
                next_fd = _open_dir_no_symlink(part, dir_fd=current_fd)
                fds_to_close.append(next_fd)
                current_fd = next_fd
                break
            except FileNotFoundError:
                if not create_missing:
                    raise
                # Create the directory; mkdir won't follow a symlink at the final component.
                try:
                    os.mkdir(part, 0o755, dir_fd=current_fd)
                except FileExistsError:
                    # Raced: something appeared; retry open which will reject symlinks.
                    continue
            except OSError as e:
                # ELOOP is a typical indicator of symlink when using O_NOFOLLOW.
                if e.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError(f"symlink or non-directory path component rejected: {part}") from e
                raise

    return current_fd, fds_to_close


def safe_write_text_under_root(root: Path, rel: str, content: str) -> None:
    """Write a file under root while resisting traversal and symlink attacks."""
    if not isinstance(content, str):
        raise ValueError(f"file contents for {rel} must be a string")

    _reject_path_traversal(rel)
    parts = Path(rel).parts
    if not parts:
        raise ValueError("empty path rejected")

    # Best-effort robust implementation on POSIX using dir_fd.
    if _dir_fd_capable():
        parent_parts = parts[:-1]
        filename = parts[-1]

        fds_to_close: list[int] = []
        try:
            leaf_fd, fds_to_close = _safe_walk_dirs(root, tuple(parent_parts), create_missing=True)

            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW

            fd = os.open(filename, flags, 0o644, dir_fd=leaf_fd)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(content)
            return
        finally:
            # Close in reverse order.
            for fd in reversed(fds_to_close):
                try:
                    os.close(fd)
                except OSError:
                    pass

    # Fallback: path-based checks.
    target = root / rel
    # Ensure parent directories exist without following symlinks (best-effort).
    current = root
    for part in Path(rel).parts[:-1]:
        current = current / part
        if current.exists():
            if current.is_symlink():
                raise ValueError(f"symlink in path rejected: {current}")
            if not current.is_dir():
                raise NotADirectoryError(f"path component is not a directory: {current}")
        else:
            try:
                current.mkdir()
            except FileExistsError:
                # Raced; re-check
                if current.is_symlink():
                    raise ValueError(f"symlink in path rejected: {current}")
                if not current.is_dir():
                    raise NotADirectoryError(f"path component is not a directory: {current}")

    # Refuse to follow a symlink as the final component where supported.
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    fd = os.open(str(target), flags, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def safe_delete_under_root(root: Path, rel: str) -> None:
    """Delete a file under root while resisting traversal and symlink attacks."""
    _reject_path_traversal(rel)
    parts = Path(rel).parts
    if not parts:
        raise ValueError("empty path rejected")

    if _dir_fd_capable():
        parent_parts = parts[:-1]
        filename = parts[-1]
        fds_to_close: list[int] = []
        try:
            try:
                leaf_fd, fds_to_close = _safe_walk_dirs(root, tuple(parent_parts), create_missing=False)
            except FileNotFoundError:
                return

            try:
                st = os.stat(filename, dir_fd=leaf_fd, follow_symlinks=False)
            except FileNotFoundError:
                return

            if stat.S_ISDIR(st.st_mode):
                raise IsADirectoryError(f"refusing to delete directory (files only): {rel}")

            # Delete without following symlinks.
            try:
                os.unlink(filename, dir_fd=leaf_fd, follow_symlinks=False)
            except TypeError:
                # Very old Python/platform: no follow_symlinks.
                os.unlink(filename, dir_fd=leaf_fd)
            return
        finally:
            for fd in reversed(fds_to_close):
                try:
                    os.close(fd)
                except OSError:
                    pass

    # Fallback: path-based.
    target = root / rel
    try:
        st = target.lstat()
    except FileNotFoundError:
        return

    if stat.S_ISDIR(st.st_mode):
        raise IsADirectoryError(f"refusing to delete directory (files only): {rel}")

    target.unlink()


def _pending_spec_changes_path(root: Path) -> Path:
    return root / ".claude" / "pending_spec_changes.json"


def _merge_pending_spec_changes(root: Path, pending: dict) -> None:
    """Persist pending spec changes for later manual review."""
    p = _pending_spec_changes_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)

    existing = {"create-or-update": {}, "delete": []}
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            # If unreadable/corrupt, overwrite.
            existing = {"create-or-update": {}, "delete": []}

    merged_create = dict(existing.get("create-or-update", {}))
    merged_create.update(pending.get("create-or-update", {}))

    merged_delete = list(dict.fromkeys([*(existing.get("delete", []) or []), *(pending.get("delete", []) or [])]))

    p.write_text(
        json.dumps({"create-or-update": merged_create, "delete": merged_delete}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _print_unified_diff(old: str, new: str, *, fromfile: str, tofile: str) -> None:
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    diff = difflib.unified_diff(old_lines, new_lines, fromfile=fromfile, tofile=tofile)
    sys.stderr.writelines(diff)


def _approve_spec_change(rel: str, *, action: str) -> bool:
    """Request per-file human approval for spec changes."""
    if not sys.stdin.isatty():
        return False
    prompt = f"Approve {action} to {rel}? Type 'yes' to approve: "
    try:
        ans = input(prompt)
    except EOFError:
        return False
    return ans.strip().lower() == "yes"


def apply_changes(changes: dict, root: Path) -> None:
    """Apply create-or-update and delete operations."""

    create_map: dict[str, str] = changes.get("create-or-update", {}) or {}
    delete_list: list[str] = changes.get("delete", []) or []

    if not isinstance(create_map, dict) or not isinstance(delete_list, list):
        raise ValueError("LLM output must include 'create-or-update' object and 'delete' list")

    # Validate paths up-front (including spec paths), so we never persist/act on traversal.
    for rel in create_map.keys():
        if not isinstance(rel, str):
            raise ValueError("create-or-update keys must be strings")
        _reject_path_traversal(rel)
    for rel in delete_list:
        if not isinstance(rel, str):
            raise ValueError("delete entries must be strings")
        _reject_path_traversal(rel)

    # Split out spec changes for separate human approval.
    normal_create: dict[str, str] = {}
    spec_create: dict[str, str] = {}
    for rel, content in create_map.items():
        if Path(rel).parts[:1] == ("specs",):
            spec_create[rel] = content
        else:
            normal_create[rel] = content

    normal_delete: list[str] = []
    spec_delete: list[str] = []
    for rel in delete_list:
        if Path(rel).parts[:1] == ("specs",):
            spec_delete.append(rel)
        else:
            normal_delete.append(rel)

    # Apply non-spec changes.
    for rel, content in normal_create.items():
        parts = Path(rel).parts
        if parts and parts[0] == ".git":
            print(f"  skipping .git path: {rel}", file=sys.stderr)
            continue

        safe_write_text_under_root(root, rel, content)
        print(f"  wrote: {rel}", file=sys.stderr)

    for rel in normal_delete:
        parts = Path(rel).parts
        if parts and parts[0] == ".git":
            print(f"  skipping .git path: {rel}", file=sys.stderr)
            continue

        safe_delete_under_root(root, rel)
        print(f"  deleted: {rel}", file=sys.stderr)

    # Handle spec changes with per-file approval.
    pending: dict = {"create-or-update": {}, "delete": []}

    for rel, new_content in spec_create.items():
        parts = Path(rel).parts
        if parts and parts[0] == ".git":
            print(f"  skipping .git path: {rel}", file=sys.stderr)
            continue

        old_content = ""
        abs_path = root / rel
        if abs_path.exists() and abs_path.is_file():
            try:
                old_content = abs_path.read_text(encoding="utf-8")
            except Exception:
                old_content = ""

        print(f"\n--- PROPOSED SPEC CHANGE: {rel} ---", file=sys.stderr)
        _print_unified_diff(old_content, new_content, fromfile=f"a/{rel}", tofile=f"b/{rel}")
        print(f"--- END PROPOSED SPEC CHANGE: {rel} ---\n", file=sys.stderr)

        if _approve_spec_change(rel, action="update/create"):
            safe_write_text_under_root(root, rel, new_content)
            print(f"  wrote (approved spec change): {rel}", file=sys.stderr)
        else:
            pending["create-or-update"][rel] = new_content
            print(f"  pending (spec change requires approval): {rel}", file=sys.stderr)

    for rel in spec_delete:
        parts = Path(rel).parts
        if parts and parts[0] == ".git":
            print(f"  skipping .git path: {rel}", file=sys.stderr)
            continue

        print(f"\n--- PROPOSED SPEC DELETE: {rel} ---", file=sys.stderr)
        if _approve_spec_change(rel, action="delete"):
            safe_delete_under_root(root, rel)
            print(f"  deleted (approved spec change): {rel}", file=sys.stderr)
        else:
            pending["delete"].append(rel)
            print(f"  pending (spec delete requires approval): {rel}", file=sys.stderr)

    if pending["create-or-update"] or pending["delete"]:
        _merge_pending_spec_changes(root, pending)
        print(
            f"\nSpec changes were not fully applied. Pending changes written to: {Path('.claude') / 'pending_spec_changes.json'}",
            file=sys.stderr,
        )
        if not sys.stdin.isatty():
            print(
                "(Non-interactive stdin detected; spec changes require human approval and were therefore deferred.)",
                file=sys.stderr,
            )


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
