"""Types for correctness-checker findings.

See src/specs/correctness-checker.md for the specification.
"""

from typing import TypedDict


class _CodeReviewFindingRequired(TypedDict):
    provenance: str
    finding: str


class CodeReviewFinding(_CodeReviewFindingRequired, total=False):
    file: str


CommandFinding = TypedDict('CommandFinding', {
    'provenance': str,
    'command': str,
    'stdout': str,
    'stderr': str,
    'exit-code': int,
})

Finding = CodeReviewFinding | CommandFinding
