"""LLM code review module.

Reviews files changed since main using GPT-5.3, returning findings
in the correctness-checker spec format.
"""

import json
import subprocess
import sys

import openai
from openai.types.responses.response_format_text_json_schema_config_param import (
    ResponseFormatTextJSONSchemaConfigParam,
)
from openai.types.responses.response_text_config_param import ResponseTextConfigParam

MODEL_NAME = 'gpt-5.3-codex'
REQUEST_TIMEOUT_SECONDS = 3600

_REVIEW_SCHEMA: dict[str, object] = {
    'type': 'object',
    'properties': {
        'comments': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'file': {'type': 'string'},
                    'finding': {'type': 'string'},
                },
                'required': ['file', 'finding'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['comments'],
    'additionalProperties': False,
}

_REVIEW_FORMAT: ResponseFormatTextJSONSchemaConfigParam = {
    'type': 'json_schema',
    'name': 'code_review',
    'strict': True,
    'schema': _REVIEW_SCHEMA,
}

_TEXT_CONFIG: ResponseTextConfigParam = {
    'format': _REVIEW_FORMAT,
}

SYSTEM_PROMPT = """\
You are a code reviewer. You will be given a git diff of changes relative to \
the main branch, along with the full contents of each changed file.

Review the changes for:
- Bugs and logic errors
- Type safety issues
- Security vulnerabilities
- Violations of the codebase's conventions

For each issue found, produce a comment with the file path and a clear \
description of the problem in Markdown. Only report genuine issues; do not \
comment on style preferences or suggest optional improvements. If the code \
looks correct, return an empty comments list.\
"""


def _changed_files() -> list[str]:
    result = subprocess.run(
        ['git', 'diff', '--no-ext-diff', 'main', '--name-only'],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return []
    return [f for f in result.stdout.strip().splitlines() if f]


def _read_file(path: str) -> str | None:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def _get_diff() -> str:
    result = subprocess.run(
        ['git', 'diff', '--no-ext-diff', 'main'],
        capture_output=True,
        text=True,
    )
    return result.stdout if result.returncode == 0 else ''


def _build_prompt(changed_files: list[str], diff: str) -> str:
    parts: list[str] = ['# Git diff\n\n```\n' + diff + '\n```\n']
    for path in changed_files:
        contents = _read_file(path)
        if contents is None:
            continue
        parts.append(f'# File: {path}\n\n```\n{contents}\n```\n')
    return '\n'.join(parts)


def review() -> list[dict[str, str]]:
    """Run LLM code review on files changed since main.

    Returns a list of correctness-checker findings with provenance 'code-review'.
    """
    changed = _changed_files()
    if not changed:
        return []

    diff = _get_diff()
    if not diff:
        return []

    user_prompt = _build_prompt(changed, diff)

    client = openai.OpenAI(timeout=REQUEST_TIMEOUT_SECONDS)
    response = client.responses.create(
        model=MODEL_NAME,
        input=[
            {'role': 'system', 'content': SYSTEM_PROMPT},
            {'role': 'user', 'content': user_prompt},
        ],
        text=_TEXT_CONFIG,
        reasoning={'effort': 'high'},
    )

    parsed = json.loads(response.output_text)
    findings: list[dict[str, str]] = []
    for comment in parsed.get('comments', []):
        finding: dict[str, str] = {
            'provenance': 'code-review',
            'finding': comment['finding'],
        }
        if comment.get('file'):
            finding['file'] = comment['file']
        findings.append(finding)

    return findings


if __name__ == '__main__':
    try:
        results = review()
        print(json.dumps(results, indent=2))
    except Exception as e:
        print(f'LLM review failed: {e}', file=sys.stderr)
        sys.exit(2)
