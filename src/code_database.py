#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Final

NL: Final[str] = "\n"

CodeDatabase = dict[str, list[list[str | None]]]


def _normalise_relative_path(raw: str) -> PurePosixPath | None:
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts:
        return None
    if any(part in ("", ".", "..") for part in path.parts):
        return None
    return path


def _find_repo_root(source_dir: Path) -> Path:
    current = source_dir.resolve()
    while True:
        if (current / ".git").exists():
            return current
        if current.parent == current:
            return source_dir.resolve()
        current = current.parent


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _list_source_files(repo_root: Path, source_dir: Path) -> list[PurePosixPath]:
    source_resolved = source_dir.resolve()
    repo_resolved = repo_root.resolve()
    source_rel_to_repo = source_resolved.relative_to(repo_resolved)

    result = _run_git(
        repo_root,
        ["ls-files", "--cached", "--others", "--exclude-standard", "--full-name", "--", str(source_rel_to_repo)],
    )

    files: list[PurePosixPath] = []
    if result.returncode == 0:
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            repo_rel = Path(stripped)
            try:
                source_rel = repo_rel.relative_to(source_rel_to_repo)
            except ValueError:
                continue
            normalised = _normalise_relative_path(source_rel.as_posix())
            if normalised is not None:
                files.append(normalised)
        return sorted(files)

    for path in source_resolved.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.resolve().relative_to(source_resolved).as_posix()
        except ValueError:
            continue
        normalised = _normalise_relative_path(rel)
        if normalised is not None:
            files.append(normalised)
    return sorted(files)


class UnrecognisedLanguageError(RuntimeError):
    def __init__(self, file_path: PurePosixPath) -> None:
        self.file_path = file_path
        super().__init__(f"Unrecognised language encountered: {file_path.as_posix()}")


def _class_signature(node: ast.ClassDef) -> str:
    bases = [ast.unparse(base) for base in node.bases]
    keywords = [
        f"{keyword.arg}={ast.unparse(keyword.value)}"
        for keyword in node.keywords
        if keyword.arg is not None
    ]
    parts = [*bases, *keywords]
    return ", ".join(parts)


def _function_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args = ast.unparse(node.args)
    returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    return f"{prefix} {node.name}({args}){returns}"


def _qualified(prefix: str, name: str) -> str:
    if not prefix:
        return name
    return f"{prefix}.{name}"


def _extract_python_definitions(source: str, file_path: PurePosixPath) -> list[list[str | None]]:
    module = ast.parse(source, filename=file_path.as_posix())
    rows: list[list[str | None]] = []

    def walk(statements: list[ast.stmt], prefix: str) -> None:
        for statement in statements:
            if isinstance(statement, ast.ClassDef):
                qualified_name = _qualified(prefix, statement.name)
                rows.append([qualified_name, _class_signature(statement), ast.get_docstring(statement)])
                walk(statement.body, qualified_name)
                continue

            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qualified_name = _qualified(prefix, statement.name)
                rows.append([qualified_name, _function_signature(statement), ast.get_docstring(statement)])
                walk(statement.body, qualified_name)

    walk(module.body, "")
    return rows


def build_code_database(source_dir: Path) -> CodeDatabase:
    source_resolved = source_dir.resolve()
    repo_root = _find_repo_root(source_resolved)
    files = _list_source_files(repo_root, source_resolved)

    database: CodeDatabase = {}

    for rel in files:
        suffix = Path(rel.name).suffix.lower()
        if suffix in {".md", ".markdown"}:
            continue
        if suffix != ".py":
            raise UnrecognisedLanguageError(rel)

        path = source_resolved / rel.as_posix()
        if path.is_symlink():
            continue

        resolved = path.resolve()
        if not resolved.is_relative_to(source_resolved):
            continue

        source = path.read_text(encoding="utf-8")
        database[rel.as_posix()] = _extract_python_definitions(source, rel)

    return database


def write_code_database(source_dir: Path, output_path: Path) -> None:
    database = build_code_database(source_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(database, indent=2, sort_keys=True) + NL, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Construct a deterministic code database for Bork")
    parser.add_argument("source_directory", type=Path)
    parser.add_argument("code_database_file", type=Path)
    args = parser.parse_args(argv)

    try:
        write_code_database(args.source_directory, args.code_database_file)
    except (OSError, SyntaxError, ValueError, UnrecognisedLanguageError) as exc:
        print(f"Failed to construct code database: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
