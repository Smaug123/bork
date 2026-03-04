#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import importlib
import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Final, cast

NL: Final[str] = chr(10)

CodeDatabase = dict[str, list[list[str | None]]]
_python_parser_cache: object | None = None


def _normalise_relative_path(raw: str) -> PurePosixPath | None:
    'Validate and normalise a repository-relative path.'
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts:
        return None
    if any(part in ('', '.', '..') for part in path.parts):
        return None
    return path


def _find_repo_root(source_dir: Path) -> Path:
    'Find the nearest ancestor containing a .git directory.'
    current = source_dir.resolve()
    while True:
        if (current / '.git').exists():
            return current
        if current.parent == current:
            return source_dir.resolve()
        current = current.parent


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    'Run a git command rooted at repo_root.'
    return subprocess.run(
        ['git', '-C', str(repo_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _list_source_files(repo_root: Path, source_dir: Path) -> list[PurePosixPath]:
    'List files under source_dir, preferring git index semantics when available.'
    source_resolved = source_dir.resolve()
    repo_resolved = repo_root.resolve()
    source_rel_to_repo = source_resolved.relative_to(repo_resolved)

    result = _run_git(
        repo_root,
        ['ls-files', '--cached', '--others', '--exclude-standard', '--full-name', '--', str(source_rel_to_repo)],
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

    for path in source_resolved.rglob('*'):
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
    'Raised when code database construction encounters a non-ingested language.'

    def __init__(self, file_path: PurePosixPath) -> None:
        self.file_path = file_path
        super().__init__(f'Unrecognised language encountered: {file_path.as_posix()}')


def _python_parser() -> object:
    'Create and cache a tree-sitter parser configured for Python.'
    global _python_parser_cache
    if _python_parser_cache is not None:
        return _python_parser_cache

    try:
        tree_sitter_module = importlib.import_module('tree_sitter')
        tree_sitter_python_module = importlib.import_module('tree_sitter_python')
    except ModuleNotFoundError as exc:
        raise ValueError('tree-sitter Python bindings are required to build the code database.') from exc

    language_ctor = getattr(tree_sitter_module, 'Language', None)
    parser_ctor = getattr(tree_sitter_module, 'Parser', None)
    language_factory = getattr(tree_sitter_python_module, 'language', None)
    if not callable(language_ctor) or not callable(parser_ctor) or not callable(language_factory):
        raise ValueError('tree-sitter modules do not expose the expected Language/Parser APIs.')

    language = language_ctor(language_factory())
    try:
        parser = parser_ctor(language)
    except TypeError:
        parser = parser_ctor()
        set_language = getattr(parser, 'set_language', None)
        if not callable(set_language):
            raise ValueError('tree-sitter Parser has no compatible constructor for language setup.')
        set_language(language)

    _python_parser_cache = parser
    return parser


def _node_type(node: object) -> str:
    'Return a node type for tree-sitter node-like objects.'
    type_obj = getattr(node, 'type', '')
    return type_obj if isinstance(type_obj, str) else ''


def _node_start(node: object) -> int:
    'Return a node start byte offset.'
    start_obj = getattr(node, 'start_byte', 0)
    return start_obj if isinstance(start_obj, int) else 0


def _node_end(node: object) -> int:
    'Return a node end byte offset.'
    end_obj = getattr(node, 'end_byte', 0)
    return end_obj if isinstance(end_obj, int) else 0


def _node_text(source_bytes: bytes, node: object) -> str:
    'Extract source text for a node byte span.'
    return source_bytes[_node_start(node):_node_end(node)].decode('utf-8')


def _named_children(node: object) -> list[object]:
    'Return named children for a node.'
    children_obj = getattr(node, 'named_children', None)
    if children_obj is None or isinstance(children_obj, (str, bytes, bytearray)):
        return []
    if not isinstance(children_obj, Sequence):
        return []
    children_seq = cast(Sequence[object], children_obj)
    return list(children_seq)


def _field(node: object, field_name: str) -> object | None:
    'Get a named field from a tree-sitter node-like object.'
    getter = getattr(node, 'child_by_field_name', None)
    if not callable(getter):
        return None
    return cast(object | None, getter(field_name))


def _definition_name(source_bytes: bytes, node: object) -> str | None:
    'Extract a class/function definition name.'
    name_node = _field(node, 'name')
    if name_node is None:
        return None
    name = _node_text(source_bytes, name_node).strip()
    return name or None


def _unwrap_decorated_definition(statement: object) -> object:
    'Return the inner definition node when a statement is decorated.'
    if _node_type(statement) != 'decorated_definition':
        return statement
    definition = _field(statement, 'definition')
    if definition is not None:
        return definition
    children = _named_children(statement)
    return children[-1] if children else statement


def _docstring_literal(source_bytes: bytes, expression: object) -> str | None:
    'Decode a docstring literal expression to a Python string.'
    expression_type = _node_type(expression)
    if expression_type == 'parenthesized_expression':
        children = _named_children(expression)
        return _docstring_literal(source_bytes, children[0]) if children else None
    if expression_type not in {'string', 'concatenated_string'}:
        return None
    try:
        value = ast.literal_eval(_node_text(source_bytes, expression))
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, str) else None


def _extract_docstring(source_bytes: bytes, body_node: object | None) -> str | None:
    'Extract the docstring from the first statement of a block.'
    if body_node is None:
        return None
    statements = _named_children(body_node)
    if not statements or _node_type(statements[0]) != 'expression_statement':
        return None
    expressions = _named_children(statements[0])
    if not expressions:
        return None
    return _docstring_literal(source_bytes, expressions[0])


def _class_signature(source_bytes: bytes, node: object) -> str:
    'Render class inheritance data from a class definition node.'
    superclasses = _field(node, 'superclasses')
    if superclasses is None:
        return ''
    signature = _node_text(source_bytes, superclasses).strip()
    if signature.startswith('(') and signature.endswith(')'):
        return signature[1:-1].strip()
    return signature


def _function_signature(source_bytes: bytes, node: object) -> str:
    'Render a function signature from a function definition node.'
    body_node = _field(node, 'body')
    if body_node is None:
        return _node_text(source_bytes, node).strip()
    header = source_bytes[_node_start(node):_node_start(body_node)].decode('utf-8').strip()
    return header[:-1].rstrip() if header.endswith(':') else header


def _qualified(prefix: str, name: str) -> str:
    'Build a qualified name by joining prefix and name.'
    return f'{prefix}.{name}' if prefix else name


def _extract_python_definitions(source: str, file_path: PurePosixPath) -> list[list[str | None]]:
    'Extract class/function definitions, signatures, and docstrings from a Python file.'
    parser = _python_parser()
    parse = getattr(parser, 'parse', None)
    if not callable(parse):
        raise ValueError('tree-sitter Parser object does not expose parse().')

    source_bytes = source.encode('utf-8')
    tree = parse(source_bytes)
    root = getattr(tree, 'root_node', None)
    if root is None:
        raise ValueError('tree-sitter parse returned no root node.')

    has_error_obj = getattr(root, 'has_error', False)
    has_error = bool(has_error_obj() if callable(has_error_obj) else has_error_obj)
    if has_error:
        raise SyntaxError(f'Unable to parse Python file: {file_path.as_posix()}')

    rows: list[list[str | None]] = []

    def walk(block_node: object, prefix: str) -> None:
        for statement in _named_children(block_node):
            definition = _unwrap_decorated_definition(statement)
            definition_type = _node_type(definition)
            if definition_type not in {'class_definition', 'function_definition'}:
                continue

            name = _definition_name(source_bytes, definition)
            if name is None:
                continue

            qualified_name = _qualified(prefix, name)
            body_node = _field(definition, 'body')
            docstring = _extract_docstring(source_bytes, body_node)

            if definition_type == 'class_definition':
                rows.append([qualified_name, _class_signature(source_bytes, definition), docstring])
            else:
                rows.append([qualified_name, _function_signature(source_bytes, definition), docstring])

            if body_node is not None:
                walk(body_node, qualified_name)

    walk(root, '')
    return rows


def build_code_database(source_dir: Path) -> CodeDatabase:
    'Build the deterministic code database for a source directory.'
    source_resolved = source_dir.resolve()
    repo_root = _find_repo_root(source_resolved)
    files = _list_source_files(repo_root, source_resolved)

    database: CodeDatabase = {}
    for rel in files:
        path = source_resolved / rel.as_posix()
        if not path.exists() or not path.is_file():
            continue
        if path.is_symlink():
            continue

        resolved = path.resolve()
        if not resolved.is_relative_to(source_resolved):
            continue

        suffix = path.suffix.lower()
        if suffix in {'.md', '.markdown'}:
            continue
        if suffix != '.py':
            raise UnrecognisedLanguageError(rel)

        source = path.read_text(encoding='utf-8')
        database[rel.as_posix()] = _extract_python_definitions(source, rel)

    return database


def write_code_database(source_dir: Path, output_path: Path) -> None:
    'Write the deterministic code database JSON file.'
    database = build_code_database(source_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(database, indent=2, sort_keys=True) + NL, encoding='utf-8')


def main(argv: list[str] | None = None) -> int:
    'Run the code database CLI entrypoint.'
    parser = argparse.ArgumentParser(description='Construct a deterministic code database for Bork')
    parser.add_argument('source_directory', type=Path)
    parser.add_argument('code_database_file', type=Path)
    args = parser.parse_args(argv)

    try:
        write_code_database(args.source_directory, args.code_database_file)
    except (OSError, SyntaxError, ValueError, UnrecognisedLanguageError) as exc:
        print(f'Failed to construct code database: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
