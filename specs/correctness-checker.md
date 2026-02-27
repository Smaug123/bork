---
kind: spec
id: core/correctness-checker
description: Defines the contract between the harness and the correctness checker that's invoked after a loop iteration.
---

The correctness checker is invoked with no arguments, with its working directory being the root of the Git repository the harness is being applied to.

It returns JSON on stdout, and an exit code of 0 if the checker has no findings, 1 if the checker has findings, and 2 if the checker somehow fails to run.

The location of the executable correctness-checker file is [as configured in the file `.config/bork.json`](./config-file.md).

# JSON output format

```json
{
    "per_file_findings": [
        { "provenance": "code-review", "file": "foo/bar.py", "finding": "The function `do_it` omits a critical safety check: ..." },
    ],
    "overall_findings": [
        { "provenance": "code-review", "finding": "The approach *works*, but fails to adhere to the design principle of dependency rejection over dependency injection: ..." }
        { "provenance": "command", "command": "uv run pyright .", "stdout": "<stdout of Pyright>", "stderr": "<stderr of Pyright>", "exit-code": 1 }
    ]
}
```

## Available types of finding

### `code-review`

Freeform text; Markdown format is encouraged.

Fields:

* the `provenance` field is exactly `code-review`;
* the `finding` field specifies the freeform text of the finding;
* if the finding is specific to a file, the field `file` specifies which file (relative to the root of the repository).

### `command`

An arbitrary command execution.

The command is expected to output UTF-8 text on stdout and stderr.
Unparseable command output is represented simply as `"<non-UTF8 output>"`.

Fields:

* the `provenance` field is exactly `command`; 
* the `command` field states what command was run;
* the `stdout` field gives the stdout;
* the `stderr` field gives the stderr;
* the `exit-code` field specifies the exit code of the command.

# Security note

The user is expected always to operate the harness (and therefore the correctness checker which the harness invokes) within a sandboxed environment, so the harness need not make any attempt to constrain the behaviour of the correctness checker to "safe" actions.
