#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final, Mapping, Sequence, cast

MAX_ITERATIONS: Final[int] = 5
MODEL_NAME: Final[str] = 'gpt-5.3-codex'
REQUEST_TIMEOUT_SECONDS: Final[int] = 3600
NON_UTF8_PLACEHOLDER: Final[str] = '<non-UTF8 output>'
DEBUG_ENV_VAR: Final[str] = 'BORK_ENABLE_DEBUG_LOG'
NL: Final[str] = chr(10)
DOUBLE_NL: Final[str] = NL + NL


@dataclass(frozen=True)
class BorkConfig:
    correctness_checker: Path | None
    edits_require_approval: set[PurePosixPath]
    not_sent: set[PurePosixPath]


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments_json: str


def _debug_enabled() -> bool:
    return os.getenv(DEBUG_ENV_VAR) == '1'


def _debug_log(message: str) -> None:
    if _debug_enabled():
        print(f'[debug] {message}', file=sys.stderr)


def _decode_utf8_or_placeholder(data: bytes) -> str:
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return NON_UTF8_PLACEHOLDER


def _find_repo_root(source_dir: Path) -> Path:
    current = source_dir.resolve()
    while True:
        if (current / '.git').exists():
            return current
        if current.parent == current:
            return source_dir.resolve()
        current = current.parent


def _normalise_relative_path(raw: str) -> PurePosixPath | None:
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts:
        return None
    if any(part in ('', '.', '..') for part in path.parts):
        return None
    return path


def _coerce_str_object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    raw_dict = cast(dict[object, object], value)
    result: dict[str, object] = {}
    for key_obj, value_obj in raw_dict.items():
        if not isinstance(key_obj, str):
            return None
        result[key_obj] = value_obj
    return result


def _coerce_object_list(value: object) -> list[object] | None:
    if not isinstance(value, list):
        return None
    return cast(list[object], value)


def _getattr_object(obj: object, attr_name: str) -> object:
    return cast(object, getattr(obj, attr_name, None))


def _load_config(repo_root: Path) -> BorkConfig:
    config_path = repo_root / '.config' / 'bork.json'
    if not config_path.exists():
        return BorkConfig(None, set(), set())

    raw_obj: object = json.loads(config_path.read_text(encoding='utf-8'))
    raw_map = _coerce_str_object_dict(raw_obj)
    if raw_map is None:
        raise ValueError('Config must be a JSON object.')

    checker_obj = raw_map.get('correctness-checker')
    checker_path: Path | None = None
    if checker_obj is not None:
        if not isinstance(checker_obj, str):
            raise ValueError('correctness-checker must be a string when provided.')
        candidate = (repo_root / checker_obj).resolve()
        if not candidate.is_relative_to(repo_root.resolve()):
            raise ValueError('correctness-checker must resolve within the Git repository root.')
        checker_path = candidate

    def parse_list(field_name: str) -> set[PurePosixPath]:
        value_obj = raw_map.get(field_name)
        if value_obj is None:
            return set()
        value_list = _coerce_object_list(value_obj)
        if value_list is None:
            raise ValueError(f'{field_name} must be a list.')

        result: set[PurePosixPath] = set()
        for item_obj in value_list:
            if not isinstance(item_obj, str):
                raise ValueError(f'{field_name} entries must be strings.')
            normalised = _normalise_relative_path(item_obj)
            if normalised is None:
                raise ValueError(f'Invalid path in {field_name}: {item_obj}')
            result.add(normalised)
        return result

    return BorkConfig(checker_path, parse_list('edits-require-approval'), parse_list('not-sent'))


def _run_git(repo_root: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ['git', '-C', str(repo_root), *args],
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

    for path in source_dir.rglob('*'):
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


def _checker_source_relative(source_dir: Path, checker_path: Path | None) -> PurePosixPath | None:
    if checker_path is None:
        return None
    try:
        rel = checker_path.resolve().relative_to(source_dir.resolve()).as_posix()
    except ValueError:
        return None
    return _normalise_relative_path(rel)


def _safe_read_bytes(source_dir: Path, rel: PurePosixPath) -> bytes | None:
    root = source_dir.resolve()
    path = root / rel.as_posix()

    if path.is_symlink():
        return None

    try:
        resolved = path.resolve()
    except OSError:
        return None

    if not resolved.is_relative_to(root):
        return None

    try:
        return path.read_bytes()
    except OSError:
        return None


def _render_codebase(
    source_dir: Path,
    files: Sequence[PurePosixPath],
    checker_rel: PurePosixPath | None,
    not_sent: set[PurePosixPath],
) -> str:
    chunks: list[str] = []
    for rel in files:
        rel_str = rel.as_posix()
        if checker_rel is not None and rel == checker_rel:
            continue

        if rel in not_sent:
            chunks.append(
                f'''--- FILE: {rel_str} ---
<contents omitted by not-sent policy>
--- END FILE: {rel_str} ---'''
            )
            continue

        file_bytes = _safe_read_bytes(source_dir, rel)
        if file_bytes is None:
            chunks.append(
                f'''--- FILE: {rel_str} ---
<contents omitted by path safety policy>
--- END FILE: {rel_str} ---'''
            )
            continue

        text = _decode_utf8_or_placeholder(file_bytes)
        chunks.append(
            f'''--- FILE: {rel_str} ---
{text}
--- END FILE: {rel_str} ---'''
        )

    return DOUBLE_NL.join(chunks)


def _specs_diff_against_main(repo_root: Path, source_dir: Path) -> str:
    source_rel_to_repo = source_dir.resolve().relative_to(repo_root.resolve())
    specs_rel = source_rel_to_repo / 'specs'

    diff_result = _run_git(repo_root, ['diff', 'main', '--', str(specs_rel)])
    if diff_result.returncode != 0:
        return ''

    sections: list[str] = []
    if diff_result.stdout.strip():
        sections.append(diff_result.stdout.rstrip(NL))

    new_specs_result = _run_git(
        repo_root,
        ['ls-files', '--others', '--exclude-standard', '--full-name', '--', str(specs_rel)],
    )
    if new_specs_result.returncode == 0:
        newly_added: list[str] = []
        for line in new_specs_result.stdout.splitlines():
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
                newly_added.append(normalised.as_posix())

        if newly_added:
            rendered = NL.join(f'{path} (newly added)' for path in sorted(set(newly_added)))
            sections.append(
                f'''--- NEW UNSTAGED SPECS FILES ---
{rendered}
--- END NEW UNSTAGED SPECS FILES ---'''
            )

    return DOUBLE_NL.join(sections)


def _extract_tool_calls(response: object) -> list[ToolCall]:
    output_obj = _getattr_object(response, 'output')
    output_list = _coerce_object_list(output_obj)
    if output_list is None:
        return []

    calls: list[ToolCall] = []
    for item in output_list:
        item_type_obj = _getattr_object(item, 'type')
        if item_type_obj != 'function_call':
            continue

        call_id_obj = _getattr_object(item, 'call_id')
        if not isinstance(call_id_obj, str):
            fallback_id_obj = _getattr_object(item, 'id')
            call_id_obj = fallback_id_obj if isinstance(fallback_id_obj, str) else None

        name_obj = _getattr_object(item, 'name')
        args_obj = _getattr_object(item, 'arguments')

        if isinstance(call_id_obj, str) and isinstance(name_obj, str):
            arguments_json = args_obj if isinstance(args_obj, str) else '{}'
            calls.append(ToolCall(call_id=call_id_obj, name=name_obj, arguments_json=arguments_json))

    return calls


def _invoke_llm(prompt: str) -> str:
    fake_output = os.getenv('BORK_FAKE_LLM_OUTPUT')
    if fake_output is not None:
        return fake_output

    openai_module = importlib.import_module('openai')
    openai_client_ctor = getattr(openai_module, 'OpenAI', None)
    if openai_client_ctor is None:
        raise RuntimeError('openai.OpenAI is unavailable.')

    client = openai_client_ctor(timeout=REQUEST_TIMEOUT_SECONDS)

    tools: list[dict[str, object]] = [
        {
            'type': 'function',
            'name': 'resolve-spec-contradiction',
            'description': 'Use when specs appear contradictory.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'spec-files': {'type': 'array', 'items': {'type': 'string'}},
                    'snippets': {'type': 'array', 'items': {'type': 'string'}},
                    'contradiction': {'type': 'string'},
                },
                'required': ['spec-files', 'snippets', 'contradiction'],
            },
        },
        {
            'type': 'function',
            'name': 'incomplete-spec',
            'description': 'Use when specs are insufficient to make a decision.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'spec-files': {'type': 'array', 'items': {'type': 'string'}},
                    'incompleteness': {'type': 'string'},
                    'degrees-of-freedom': {'type': 'array', 'items': {'type': 'string'}},
                },
                'required': ['spec-files', 'incompleteness', 'degrees-of-freedom'],
            },
        },
    ]

    current_input: object = prompt
    previous_response_id: str | None = None

    while True:
        _debug_log(f'LLM request input: {current_input!r}')
        with client.responses.stream(
            model=MODEL_NAME,
            input=current_input,
            previous_response_id=previous_response_id,
            tools=tools,
            reasoning={'effort': 'high'},
        ) as stream:
            for event in stream:
                _debug_log(f'LLM stream event: {event!r}')
            response = stream.get_final_response()

        output_text_obj = _getattr_object(response, 'output_text')
        output_text = output_text_obj if isinstance(output_text_obj, str) else ''
        _debug_log(f'LLM response text: {output_text}')

        tool_calls = _extract_tool_calls(response)
        if not tool_calls:
            return output_text

        outputs: list[dict[str, str]] = []
        for call in tool_calls:
            print(
                f'''Tool call requested: {call.name}
Arguments:
{call.arguments_json}
Enter tool result:''',
                file=sys.stderr,
            )
            tool_result = input()
            outputs.append({'type': 'function_call_output', 'call_id': call.call_id, 'output': tool_result})

        response_id_obj = _getattr_object(response, 'id')
        if not isinstance(response_id_obj, str):
            raise RuntimeError('Missing response id for tool-calling continuation.')
        previous_response_id = response_id_obj
        current_input = outputs


def _parse_plan(raw: str) -> tuple[str, list[str], dict[PurePosixPath, str], list[PurePosixPath]]:
    parsed_obj: object = json.loads(raw)
    parsed_map = _coerce_str_object_dict(parsed_obj)
    if parsed_map is None:
        raise ValueError('LLM response must be a JSON object.')

    high_level_obj = parsed_map.get('high-level-description', '')
    if not isinstance(high_level_obj, str):
        raise ValueError('high-level-description must be a string.')

    decisions_obj = parsed_map.get('implementation-decisions', [])
    decisions_list = _coerce_object_list(decisions_obj)
    if decisions_list is None:
        raise ValueError('implementation-decisions must be a list.')

    implementation_decisions: list[str] = []
    for decision_obj in decisions_list:
        if not isinstance(decision_obj, str):
            raise ValueError('implementation-decisions entries must be strings.')
        implementation_decisions.append(decision_obj)

    create_obj = parsed_map.get('create-or-update', {})
    create_map = _coerce_str_object_dict(create_obj)
    if create_map is None:
        raise ValueError('create-or-update must be an object.')

    create: dict[PurePosixPath, str] = {}
    for path_raw, value_obj in create_map.items():
        rel = _normalise_relative_path(path_raw)
        if rel is None:
            continue
        value_map = _coerce_str_object_dict(value_obj)
        if value_map is None:
            continue
        contents_obj = value_map.get('contents')
        if isinstance(contents_obj, str):
            create[rel] = contents_obj

    delete_obj = parsed_map.get('delete', [])
    delete_list = _coerce_object_list(delete_obj)
    if delete_list is None:
        raise ValueError('delete must be a list.')

    deletes: list[PurePosixPath] = []
    for item_obj in delete_list:
        item_map = _coerce_str_object_dict(item_obj)
        if item_map is None:
            continue
        file_obj = item_map.get('file')
        if isinstance(file_obj, str):
            rel = _normalise_relative_path(file_obj)
            if rel is not None:
                deletes.append(rel)

    return high_level_obj, implementation_decisions, create, deletes


def _print_llm_commentary(high_level_description: str, implementation_decisions: Sequence[str]) -> None:
    if high_level_description:
        print(
            f'''LLM high-level description:
{high_level_description}''',
            file=sys.stderr,
        )

    if not implementation_decisions:
        return

    print('LLM implementation decisions:', file=sys.stderr)
    for decision in implementation_decisions:
        print(f'- {decision}', file=sys.stderr)


def _ask_approval(prompt: str) -> bool:
    print(f'{prompt} [y/N]', file=sys.stderr)
    return input().strip().lower() in {'y', 'yes'}


def _validated_target(source_dir: Path, rel: PurePosixPath) -> Path:
    root = source_dir.resolve()
    target = root / rel.as_posix()

    cursor = root
    for part in rel.parts[:-1]:
        cursor = cursor / part
        if cursor.exists() and cursor.is_symlink():
            raise RuntimeError(f'Refusing symlinked parent path: {cursor}')

    if target.exists() and target.is_symlink():
        raise RuntimeError(f'Refusing symlinked target path: {target}')

    if not target.parent.resolve().is_relative_to(root):
        raise RuntimeError(f'Refusing path outside source directory: {target}')

    return target


def _apply_plan(
    source_dir: Path,
    create: Mapping[PurePosixPath, str],
    deletes: Sequence[PurePosixPath],
    config: BorkConfig,
    checker_rel: PurePosixPath | None,
) -> None:
    for rel, contents in create.items():
        if rel in config.not_sent:
            continue

        if checker_rel is not None and rel == checker_rel:
            print(
                f'''Refused immutable checker edit for {rel}:
{contents}''',
                file=sys.stderr,
            )
            continue

        needs_approval = (rel.parts[0] == 'specs') or (rel in config.edits_require_approval)
        if needs_approval and not _ask_approval(f'Approve write to {rel.as_posix()}?'):
            continue

        target = _validated_target(source_dir, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding='utf-8')

    for rel in deletes:
        if rel in config.not_sent:
            continue

        if checker_rel is not None and rel == checker_rel:
            print(f'Refused immutable checker delete for {rel}', file=sys.stderr)
            continue

        needs_approval = (rel.parts[0] == 'specs') or (rel in config.edits_require_approval)
        if needs_approval and not _ask_approval(f'Approve delete of {rel.as_posix()}?'):
            continue

        target = _validated_target(source_dir, rel)
        if target.exists() and target.is_file():
            target.unlink()


def _run_correctness_checker(repo_root: Path, checker_path: Path) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [str(checker_path)],
            cwd=repo_root,
            check=False,
            stdout=subprocess.PIPE,
        )
    except OSError as exc:
        synthetic_failure = {
            'per_file_findings': [],
            'overall_findings': [
                {
                    'provenance': 'code-review',
                    'finding': f'Failed to run correctness checker: {exc}',
                }
            ],
        }
        return True, json.dumps(synthetic_failure)

    stdout = _decode_utf8_or_placeholder(proc.stdout)
    return proc.returncode != 0, stdout


def run(source_dir: Path) -> int:
    source_dir = source_dir.resolve()
    repo_root = _find_repo_root(source_dir)
    config = _load_config(repo_root)
    checker_rel = _checker_source_relative(source_dir, config.correctness_checker)

    previous_checker_output: str | None = None

    for iteration in range(1, MAX_ITERATIONS + 1):
        files = _list_source_files(repo_root, source_dir)
        codebase = _render_codebase(source_dir, files, checker_rel, config.not_sent)
        specs_diff = _specs_diff_against_main(repo_root, source_dir)

        prompt_parts = [
            'You are a coding agent reconciling code and specs.',
            'Do not assume any code is currently correct.',
            'Changes to specs are a last resort unless specs contradict each other.',
            'If specs are contradictory or incomplete, use the provided tools.',
            'Respond with ONLY a JSON object with keys high-level-description, implementation-decisions, create-or-update, and delete.',
            codebase,
        ]

        if specs_diff:
            prompt_parts.append(
                f'''--- SPECS DIFF VS main ---
{specs_diff}
--- END SPECS DIFF VS main ---'''
            )

        if previous_checker_output is not None:
            prompt_parts.append(
                f'''--- CORRECTNESS CHECKER OUTPUT ---
{previous_checker_output}
--- END CORRECTNESS CHECKER OUTPUT ---'''
            )

        prompt = DOUBLE_NL.join(prompt_parts)
        llm_raw = _invoke_llm(prompt)
        high_level_description, implementation_decisions, create, deletes = _parse_plan(llm_raw)
        _print_llm_commentary(high_level_description, implementation_decisions)
        _apply_plan(source_dir, create, deletes, config, checker_rel)

        if config.correctness_checker is None:
            return 0

        has_findings, checker_output = _run_correctness_checker(repo_root, config.correctness_checker)
        if not has_findings:
            return 0

        previous_checker_output = checker_output

        if iteration == MAX_ITERATIONS:
            print('Reached iteration limit with remaining findings; human intervention required.', file=sys.stderr)
            return 1

    return 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Bork reconciliation harness')
    parser.add_argument('source_directory', type=Path)
    args = parser.parse_args(argv)
    return run(args.source_directory)


if __name__ == '__main__':
    raise SystemExit(main())
