#!/usr/bin/env python3
"""Coding harness: reads specs and codebase, asks an LLM to bring codebase into compliance.

Implements the reconciliation loop defined in specs/edit-loop.md.

Notably:
- Omits `.gitignore`'d files from the prompt.
- Does not modify `.git/`.
- Does not modify `.config/bork.json` (user configuration).
- Requires per-change human approval for edits to `specs/`.
- Requires per-change human approval for edits to any paths configured in `.config/bork.json`'s
  `edits-require-approval` list, and for edits to the configured correctness checker executable.
- Can run an optional correctness checker configured in `.config/bork.json`.
"""

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

CONFIG_REL_PATH = ".config/bork.json"
PROTECTED_REL_PATHS = {CONFIG_REL_PATH}

# Per specs/llm-api-usage.md
LLM_MODEL = "gpt-5.3-codex"
LLM_REASONING_EFFORT = "high"


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
    appended_text: str = "",
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
    if appended_text.strip():
        joined = "\n\n".join([joined, appended_text.strip()])

    return (
        "You are a coding agent. Below is the entire contents of a repository, "
        "including specification documents in specs/*.md.\n\n"
        "Your job: determine what changes are needed to bring the codebase into "
        "compliance with the specs.\n\n"
        "Do not assume that any given piece of code is currently correct. "
        "Treat the current codebase and the specs as potentially divergent, and reconcile them.\n\n"
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


def _normalize_rel_path(rel: str) -> str:
    """Normalize a validated relative path for comparisons.

    This collapses redundant separators (e.g. ".config//bork.json"), without allowing
    traversal (which is rejected earlier).
    """
    return PurePath(rel).as_posix()


def _dir_fd_capable() -> bool:
    """Return True if this platform/Python supports *at() style operations via dir_fd."""
    # We test whether os.open accepts the keyword-only dir_fd parameter.
    # IMPORTANT: close the fd on success to avoid leaking descriptors.
    try:
        fd = os.open(".", os.O_RDONLY, dir_fd=None)  # type: ignore[arg-type]
    except TypeError:
        return False
    except OSError:
        # If this errors for reasons other than TypeError, dir_fd is likely supported.
        return True
    else:
        try:
            os.close(fd)
        except OSError:
            pass
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

    payload = json.dumps({"create-or-update": merged_create, "delete": merged_delete}, indent=2, sort_keys=True) + "\n"
    safe_write_text_under_root(root, ".claude/pending_spec_changes.json", payload)


def _pending_human_approval_changes_path(root: Path) -> Path:
    return root / ".claude" / "pending_human_approval.json"


def _merge_pending_human_approval_changes(root: Path, pending: dict) -> None:
    """Persist pending non-spec changes which require human approval."""
    p = _pending_human_approval_changes_path(root)

    existing = {"create-or-update": {}, "delete": []}
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            existing = {"create-or-update": {}, "delete": []}

    merged_create = dict(existing.get("create-or-update", {}))
    merged_create.update(pending.get("create-or-update", {}))

    merged_delete = list(dict.fromkeys([*(existing.get("delete", []) or []), *(pending.get("delete", []) or [])]))

    payload = json.dumps({"create-or-update": merged_create, "delete": merged_delete}, indent=2, sort_keys=True) + "\n"
    safe_write_text_under_root(root, ".claude/pending_human_approval.json", payload)


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


def _approve_human_required_change(rel: str, *, action: str) -> bool:
    """Request per-file human approval for non-spec changes that require approval."""
    if not sys.stdin.isatty():
        return False
    prompt = f"Approve {action} to {rel}? Type 'yes' to approve: "
    try:
        ans = input(prompt)
    except EOFError:
        return False
    return ans.strip().lower() == "yes"


def _is_protected_path(rel: str) -> bool:
    # Paths are already traversal-checked and backslashes are rejected.
    return _normalize_rel_path(rel) in PROTECTED_REL_PATHS


def _normalize_configured_repo_path(raw: object) -> str | None:
    """Normalize a repo-relative path sourced from user config.

    The config examples use a leading "./" (e.g. "./correctness.py"). We accept and strip
    that prefix, while still rejecting absolute paths, backslashes, NUL bytes, and traversal
    via "..". We also reject embedded "." path segments (e.g. "foo/./bar").

    Returns a normalized posix relative path, or None if invalid.
    """
    if not isinstance(raw, str):
        return None

    s = raw.strip()
    if not s:
        return None

    if "\x00" in s or "\\" in s:
        return None

    # Accept and strip one or more leading "./".
    while s.startswith("./"):
        s = s[2:]

    if s in {"", "."}:
        return None

    p = PurePath(s)

    if p.is_absolute() or getattr(p, "drive", ""):
        return None

    if any(part in {"..", "."} for part in p.parts):
        return None

    return p.as_posix()


def _approval_requirements_from_config(root: Path) -> tuple[bool, set[str]]:
    """Return (require_approval_for_all_edits, normalized_paths_requiring_approval)."""
    cfg, err = _read_bork_config(root)

    # If config exists but is invalid/unreadable, we conservatively require approval for all edits.
    if err is not None:
        print(f"Warning: {err}. Requiring human approval for all edits.", file=sys.stderr)
        return True, set()

    if cfg is None:
        return False, set()

    require_all = False
    required: set[str] = set()

    # edits-require-approval
    era = cfg.get("edits-require-approval", [])
    if era is None:
        era = []
    if not isinstance(era, list):
        print(
            f"Warning: {CONFIG_REL_PATH} field 'edits-require-approval' must be a list of strings. "
            "Requiring human approval for all edits.",
            file=sys.stderr,
        )
        require_all = True
    else:
        for item in era:
            normalized = _normalize_configured_repo_path(item)
            if normalized is None:
                print(
                    f"Warning: ignoring invalid entry in edits-require-approval: {item!r} (must be a safe repo-relative path)",
                    file=sys.stderr,
                )
                continue
            if normalized in PROTECTED_REL_PATHS:
                # The config file itself is always immutable.
                continue
            required.add(normalized)

    # correctness-checker executable requires approval if configured.
    checker = cfg.get("correctness-checker")
    if isinstance(checker, str) and checker.strip():
        normalized = _normalize_configured_repo_path(checker)
        if normalized is None:
            # Don't force require_all for this; just warn.
            print(
                f"Warning: configured correctness-checker path is not a safe repo-relative path: {checker!r}. "
                "Cannot enforce approval for that path.",
                file=sys.stderr,
            )
        else:
            if normalized not in PROTECTED_REL_PATHS:
                required.add(normalized)

    return require_all, required


def _read_text_best_effort(p: Path) -> str:
    try:
        if p.exists() and p.is_file():
            return p.read_text(encoding="utf-8")
    except Exception:
        return ""
    return ""


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

    require_all_approval, approval_paths = _approval_requirements_from_config(root)

    def _requires_human_approval(rel: str) -> bool:
        if require_all_approval:
            return True
        return _normalize_rel_path(rel) in approval_paths

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

    # Pending buckets.
    pending_human: dict = {"create-or-update": {}, "delete": []}

    # Apply non-spec changes.
    for rel, content in normal_create.items():
        parts = Path(rel).parts
        if parts and parts[0] == ".git":
            print(f"  skipping .git path: {rel}", file=sys.stderr)
            continue
        if _is_protected_path(rel):
            print(f"  skipping protected config path: {rel}", file=sys.stderr)
            continue

        if _requires_human_approval(rel):
            old_content = _read_text_best_effort(root / rel)
            print(f"\n--- PROPOSED CHANGE (REQUIRES APPROVAL): {rel} ---", file=sys.stderr)
            _print_unified_diff(old_content, content, fromfile=f"a/{rel}", tofile=f"b/{rel}")
            print(f"--- END PROPOSED CHANGE: {rel} ---\n", file=sys.stderr)

            if _approve_human_required_change(rel, action="update/create"):
                safe_write_text_under_root(root, rel, content)
                print(f"  wrote (approved): {rel}", file=sys.stderr)
            else:
                pending_human["create-or-update"][rel] = content
                print(f"  pending (human approval required): {rel}", file=sys.stderr)
            continue

        safe_write_text_under_root(root, rel, content)
        print(f"  wrote: {rel}", file=sys.stderr)

    for rel in normal_delete:
        parts = Path(rel).parts
        if parts and parts[0] == ".git":
            print(f"  skipping .git path: {rel}", file=sys.stderr)
            continue
        if _is_protected_path(rel):
            print(f"  skipping protected config path: {rel}", file=sys.stderr)
            continue

        if _requires_human_approval(rel):
            old_content = _read_text_best_effort(root / rel)
            print(f"\n--- PROPOSED DELETE (REQUIRES APPROVAL): {rel} ---", file=sys.stderr)
            _print_unified_diff(old_content, "", fromfile=f"a/{rel}", tofile=f"b/{rel}")
            print(f"--- END PROPOSED DELETE: {rel} ---\n", file=sys.stderr)

            if _approve_human_required_change(rel, action="delete"):
                try:
                    safe_delete_under_root(root, rel)
                except IsADirectoryError:
                    print(f"  skipping directory delete request (approved): {rel}", file=sys.stderr)
                else:
                    print(f"  deleted (approved): {rel}", file=sys.stderr)
            else:
                pending_human["delete"].append(rel)
                print(f"  pending delete (human approval required): {rel}", file=sys.stderr)
            continue

        try:
            safe_delete_under_root(root, rel)
        except IsADirectoryError:
            # Spec says delete files; if the model requests directory deletion, ignore.
            print(f"  skipping directory delete request: {rel}", file=sys.stderr)
            continue
        print(f"  deleted: {rel}", file=sys.stderr)

    if pending_human["create-or-update"] or pending_human["delete"]:
        _merge_pending_human_approval_changes(root, pending_human)
        print(
            f"\nSome changes require human approval and were not applied. Pending changes written to: {Path('.claude') / 'pending_human_approval.json'}",
            file=sys.stderr,
        )
        if not sys.stdin.isatty():
            print(
                "(Non-interactive stdin detected; approval-required changes were therefore deferred.)",
                file=sys.stderr,
            )

    # Handle spec changes with per-file approval.
    pending_spec: dict = {"create-or-update": {}, "delete": []}

    for rel, new_content in spec_create.items():
        parts = Path(rel).parts
        if parts and parts[0] == ".git":
            print(f"  skipping .git path: {rel}", file=sys.stderr)
            continue

        old_content = _read_text_best_effort(root / rel)

        print(f"\n--- PROPOSED SPEC CHANGE: {rel} ---", file=sys.stderr)
        _print_unified_diff(old_content, new_content, fromfile=f"a/{rel}", tofile=f"b/{rel}")
        print(f"--- END PROPOSED SPEC CHANGE: {rel} ---\n", file=sys.stderr)

        if _approve_spec_change(rel, action="update/create"):
            safe_write_text_under_root(root, rel, new_content)
            print(f"  wrote (approved spec change): {rel}", file=sys.stderr)
        else:
            pending_spec["create-or-update"][rel] = new_content
            print(f"  pending (spec change requires approval): {rel}", file=sys.stderr)

    for rel in spec_delete:
        parts = Path(rel).parts
        if parts and parts[0] == ".git":
            print(f"  skipping .git path: {rel}", file=sys.stderr)
            continue

        old_content = _read_text_best_effort(root / rel)
        print(f"\n--- PROPOSED SPEC DELETE: {rel} ---", file=sys.stderr)
        _print_unified_diff(old_content, "", fromfile=f"a/{rel}", tofile=f"b/{rel}")
        print(f"--- END PROPOSED SPEC DELETE: {rel} ---\n", file=sys.stderr)

        if _approve_spec_change(rel, action="delete"):
            try:
                safe_delete_under_root(root, rel)
            except IsADirectoryError:
                print(f"  skipping directory delete request (approved spec delete): {rel}", file=sys.stderr)
            else:
                print(f"  deleted (approved spec change): {rel}", file=sys.stderr)
        else:
            pending_spec["delete"].append(rel)
            print(f"  pending (spec delete requires approval): {rel}", file=sys.stderr)

    if pending_spec["create-or-update"] or pending_spec["delete"]:
        _merge_pending_spec_changes(root, pending_spec)
        print(
            f"\nSpec changes were not fully applied. Pending changes written to: {Path('.claude') / 'pending_spec_changes.json'}",
            file=sys.stderr,
        )
        if not sys.stdin.isatty():
            print(
                "(Non-interactive stdin detected; spec changes require human approval and were therefore deferred.)",
                file=sys.stderr,
            )


def _read_bork_config(root: Path) -> tuple[dict | None, str | None]:
    """Read `.config/bork.json` if present.

    Returns: (config_dict_or_none, error_string_or_none)
    """
    cfg_path = root / CONFIG_REL_PATH
    if not cfg_path.exists():
        return None, None

    try:
        raw = cfg_path.read_text(encoding="utf-8")
    except Exception as e:
        return None, f"failed to read {CONFIG_REL_PATH}: {e}"

    try:
        data = json.loads(raw)
    except Exception as e:
        return None, f"invalid JSON in {CONFIG_REL_PATH}: {e}"

    if not isinstance(data, dict):
        return None, f"{CONFIG_REL_PATH} must contain a JSON object"

    return data, None


def _correctness_checker_configured_for_loop(root: Path) -> bool:
    """Return True iff a correctness checker appears to be configured.

    This is used to implement the edit-loop rule:
      - Only loop once when there is no correctness checker.

    If the config file exists but is unreadable/invalid, we conservatively return True
    (treating the project as intending to use a checker).
    """
    cfg_path = root / CONFIG_REL_PATH
    if not cfg_path.exists():
        return False

    try:
        raw = cfg_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return True

    if not isinstance(data, dict):
        return True

    if "correctness-checker" not in data:
        return False

    # If explicitly null, treat as not configured.
    if data.get("correctness-checker") is None:
        return False

    return True


def _decode_utf8_or_placeholder(b: bytes) -> str:
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return "<non-UTF8 output>"


def _format_block_for_prompt(title: str, payload: dict) -> str:
    return "\n".join(
        [
            f"--- {title} ---",
            json.dumps(payload, indent=2, sort_keys=True),
            f"--- END {title} ---",
        ]
    )


def _run_correctness_checker(root: Path, checker_cmd: str) -> tuple[bool, str]:
    """Run the configured correctness checker and interpret its result.

    Contract (per specs/correctness-checker.md):
      - invoked with no arguments in repo root
      - exit 0: no findings
      - exit 1: findings
      - exit 2: failed to run
      - stdout: JSON
    """
    if not isinstance(checker_cmd, str) or checker_cmd.strip() == "":
        payload = {
            "checker": checker_cmd,
            "assessment": "failed-to-run",
            "summary": "Invalid correctness-checker value (must be a non-empty string).",
        }
        return False, _format_block_for_prompt("CORRECTNESS CHECKER OUTPUT", payload)

    try:
        proc = subprocess.run(
            [checker_cmd],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as e:
        payload = {
            "checker": checker_cmd,
            "exit-code": 2,
            "assessment": "failed-to-run",
            "summary": "Correctness checker could not be executed (FileNotFoundError).",
            "harness-error": str(e),
        }
        return False, _format_block_for_prompt("CORRECTNESS CHECKER OUTPUT", payload)
    except PermissionError as e:
        payload = {
            "checker": checker_cmd,
            "exit-code": 2,
            "assessment": "failed-to-run",
            "summary": "Correctness checker could not be executed (PermissionError).",
            "harness-error": str(e),
        }
        return False, _format_block_for_prompt("CORRECTNESS CHECKER OUTPUT", payload)
    except OSError as e:
        payload = {
            "checker": checker_cmd,
            "exit-code": 2,
            "assessment": "failed-to-run",
            "summary": "Correctness checker could not be executed (OSError).",
            "harness-error": str(e),
        }
        return False, _format_block_for_prompt("CORRECTNESS CHECKER OUTPUT", payload)

    stdout = _decode_utf8_or_placeholder(proc.stdout)
    stderr = _decode_utf8_or_placeholder(proc.stderr)
    exit_code = proc.returncode

    parsed: object | None = None
    parse_error: str | None = None

    # Spec requires JSON on stdout; treat missing/invalid JSON as a contract violation.
    if stdout == "<non-UTF8 output>":
        parse_error = "stdout was not valid UTF-8"
    else:
        if not stdout.strip():
            parse_error = "stdout was empty; expected JSON"
        else:
            try:
                parsed = json.loads(stdout)
            except Exception as e:
                parse_error = f"stdout was not valid JSON: {e}"

    if parse_error is None and not isinstance(parsed, dict):
        parse_error = f"stdout JSON was not an object (got {type(parsed).__name__})"

    per_file_findings = None
    overall_findings = None
    if isinstance(parsed, dict):
        per_file_findings = parsed.get("per_file_findings")
        overall_findings = parsed.get("overall_findings")

    findings_count = 0
    if isinstance(per_file_findings, list):
        findings_count += len(per_file_findings)
    if isinstance(overall_findings, list):
        findings_count += len(overall_findings)

    assessment = "unexpected"
    ok = False

    if exit_code == 0:
        if parse_error is not None:
            assessment = "failed-to-run"
            ok = False
        elif findings_count > 0:
            assessment = "findings"
            ok = False
        else:
            assessment = "no-findings"
            ok = True
    elif exit_code == 1:
        # Findings are only meaningful if we can parse the JSON payload.
        assessment = "findings" if parse_error is None else "failed-to-run"
        ok = False
    elif exit_code == 2:
        assessment = "failed-to-run"
        ok = False
    else:
        assessment = "unexpected-exit-code"
        ok = False

    payload: dict = {
        "checker": checker_cmd,
        "exit-code": exit_code,
        "assessment": assessment,
        "stdout": stdout,
        "stderr": stderr,
        "parsed": parsed if isinstance(parsed, dict) else None,
    }
    if parse_error is not None:
        payload["parse-error"] = parse_error
    if isinstance(per_file_findings, list) or isinstance(overall_findings, list):
        payload["findings-count"] = findings_count

    if ok:
        return True, f"correctness checker OK (exit 0, no findings): {checker_cmd}"

    # Failure: return a structured block for the next loop to consume.
    return False, _format_block_for_prompt("CORRECTNESS CHECKER OUTPUT", payload)


def run_correctness_checks(root: Path) -> tuple[bool, str, bool]:
    """Run correctness checks.

    If `.config/bork.json` exists and configures `correctness-checker`, run it.

    Returns:
      (ok, details, checker_was_configured)

    Where:
      - ok is True iff checks passed / produced no findings.
      - details is a human/LLM-consumable string.
      - checker_was_configured is True iff a `correctness-checker` command was present.

    If no checker is configured, ok=True and checker_was_configured=False.
    """
    cfg, err = _read_bork_config(root)
    if err is not None:
        payload = {
            "config": CONFIG_REL_PATH,
            "assessment": "failed-to-run",
            "summary": err,
        }
        return False, _format_block_for_prompt("CORRECTNESS CHECKER OUTPUT", payload), False

    if cfg is None:
        return True, f"no {CONFIG_REL_PATH}; skipping correctness checker", False

    checker = cfg.get("correctness-checker")
    if checker is None:
        return True, "no correctness-checker configured; skipping correctness checker", False

    if not isinstance(checker, str):
        payload = {
            "config": CONFIG_REL_PATH,
            "assessment": "failed-to-run",
            "summary": "Field 'correctness-checker' must be a string.",
            "value": checker,
        }
        return False, _format_block_for_prompt("CORRECTNESS CHECKER OUTPUT", payload), False

    ok, details = _run_correctness_checker(root, checker)
    return ok, details, True


def _wants_changes(changes: dict) -> bool:
    create_map = changes.get("create-or-update", {}) or {}
    delete_list = changes.get("delete", []) or []
    if not isinstance(create_map, dict) or not isinstance(delete_list, list):
        raise ValueError("LLM output must include 'create-or-update' object and 'delete' list")
    return bool(create_map) or bool(delete_list)


def _extract_responses_api_output_text(response: object) -> str | None:
    """Best-effort extraction of a text payload from the Responses API result."""

    # Newer SDKs expose an aggregated string.
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip() != "":
        return output_text

    # Some SDKs return dict-like payloads.
    if isinstance(response, dict):
        ot = response.get("output_text")
        if isinstance(ot, str) and ot.strip() != "":
            return ot

    # Try to stitch together content blocks.
    out = getattr(response, "output", None)
    if out is None and isinstance(response, dict):
        out = response.get("output")

    if not isinstance(out, list):
        return None

    texts: list[str] = []

    for item in out:
        content = None
        if isinstance(item, dict):
            content = item.get("content")
        else:
            content = getattr(item, "content", None)

        if not isinstance(content, list):
            continue

        for block in content:
            if isinstance(block, dict):
                # Common shape: {"type": "output_text", "text": "..."}
                t = block.get("text")
                if isinstance(t, str):
                    texts.append(t)
                continue

            t = getattr(block, "text", None)
            if isinstance(t, str):
                texts.append(t)

    if not texts:
        return None

    return "".join(texts)


def _invoke_llm_via_responses_api(client: OpenAI, prompt: str) -> str:
    """Invoke the LLM using the OpenAI Responses API.

    Per specs/llm-api-usage.md:
      - model: gpt-5.3-codex
      - reasoning: high
      - timeout: configured on the client

    We request a JSON object output format.

    Note: Codex-family models are not chat models; they must use the Responses API.
    """

    # Try a small set of argument spellings for compatibility across SDK versions,
    # while preserving the same required semantics.
    attempts: list[dict] = [
        {
            "model": LLM_MODEL,
            "input": prompt,
            "reasoning": {"effort": LLM_REASONING_EFFORT},
            "text": {"format": {"type": "json_object"}},
        },
        {
            "model": LLM_MODEL,
            "input": prompt,
            "reasoning": {"effort": LLM_REASONING_EFFORT},
            "response_format": {"type": "json_object"},
        },
        {
            "model": LLM_MODEL,
            "input": prompt,
            "reasoning_effort": LLM_REASONING_EFFORT,
            "text": {"format": {"type": "json_object"}},
        },
        {
            "model": LLM_MODEL,
            "input": prompt,
            "reasoning_effort": LLM_REASONING_EFFORT,
            "response_format": {"type": "json_object"},
        },
    ]

    last_type_error: Exception | None = None

    for kwargs in attempts:
        try:
            response = client.responses.create(**kwargs)
            raw = _extract_responses_api_output_text(response)
            if raw is None or raw.strip() == "":
                raise ValueError("LLM returned empty output text")
            return raw
        except TypeError as e:
            # Different OpenAI SDK versions may not accept some kwargs.
            last_type_error = e
            continue

    raise TypeError(
        "OpenAI Responses API invocation failed due to incompatible client library "
        "(did not accept required arguments for reasoning effort and/or JSON formatting)."
    ) from last_type_error


def main() -> None:
    root = Path.cwd()

    # Spec: only loop once when there is no correctness checker configured.
    checker_configured_for_loop = _correctness_checker_configured_for_loop(root)
    max_iterations = 5 if checker_configured_for_loop else 1

    appended_failures: list[str] = []

    # Spec: use the most advanced OpenAI model (currently gpt-5.3-codex) with high reasoning,
    # and a 1 hour timeout on requests.
    client = OpenAI(timeout=60 * 60)

    for iteration in range(1, max_iterations + 1):
        specs_baseline, specs_diff_text, untracked_specs = compute_specs_status(root)
        newly_added_specs = set(untracked_specs)

        files = collect_files(root)

        appendix_parts: list[str] = [
            "--- HARNESS CONTEXT ---",
            f"Iteration: {iteration} / {max_iterations}",
            f"Protected (never edited) path: {CONFIG_REL_PATH}",
            f"Correctness checker configured (controls loop mode): {checker_configured_for_loop}",
            "--- END HARNESS CONTEXT ---",
        ]
        if appended_failures:
            appendix_parts.append("--- CORRECTNESS CHECK FAILURES (most recent last) ---")
            appendix_parts.extend(appended_failures)
            appendix_parts.append("--- END CORRECTNESS CHECK FAILURES ---")

        prompt = build_prompt(
            files,
            specs_baseline_ref=specs_baseline,
            specs_diff_text=specs_diff_text,
            newly_added_specs=newly_added_specs,
            appended_text="\n".join(appendix_parts),
        )

        print(f"Collected {len(files)} files; iteration {iteration}/{max_iterations}; sending to LLM...", file=sys.stderr)

        raw = _invoke_llm_via_responses_api(client, prompt)

        changes = json.loads(raw)

        if _wants_changes(changes):
            print("Applying changes...", file=sys.stderr)
            apply_changes(changes, root)

            ok, details, checker_was_configured = run_correctness_checks(root)

            # Spec: only loop once when there is no correctness checker.
            if not checker_configured_for_loop:
                print(f"Correctness checks: {details}", file=sys.stderr)
                print("No correctness checker configured; single iteration complete.", file=sys.stderr)
                return

            # Spec: if five iterations take place and the model is still requesting changes,
            # apply those changes and then break out, requesting human intervention.
            if iteration >= max_iterations:
                print(
                    "Cycle limit reached (5 iterations) and model is still requesting changes. "
                    "Latest changes were applied; human intervention requested.",
                    file=sys.stderr,
                )
                return

            if not ok:
                appended_failures.append(details)
                print("Correctness checks failed; commencing next loop.", file=sys.stderr)
                continue

            print(f"Correctness checks: {details}", file=sys.stderr)

            # Spec: if there are no findings from the correctness checker after a change is applied,
            # the loop ends.
            if checker_was_configured:
                print("No findings from correctness checker; ending loop.", file=sys.stderr)
                return

            # Defensive: checker_configured_for_loop should imply a checker is configured.
            continue

        # No changes requested by the model.
        ok, details, _checker_was_configured = run_correctness_checks(root)
        if ok:
            print(f"Converged. Correctness checks: {details}", file=sys.stderr)
            return

        appended_failures.append(details)
        print("Model requested no changes, but correctness checks failed; commencing next loop.", file=sys.stderr)

    # Defensive: loop should have returned.
    print("Cycle limit reached; human intervention requested.", file=sys.stderr)


if __name__ == "__main__":
    main()
