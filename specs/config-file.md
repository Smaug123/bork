---
kind: spec
id: core/config-file
description: Defines the user-written file that configures the Bork system.
---

There are a few knobs the user can turn in the Bork system; those knobs are configured in the `.config/bork.json` file at the root of the repository.

# JSON file format

Fields are all optional.

```json
{
    "correctness-checker": "./correctness.py",
    "edits-require-approval": ["flake.nix", "flake.lock"],
    "not-sent": ["uv.lock"]
}
```

## `correctness-checker`

This field configures the location of the executable file which forms the [correctness checker](./correctness-checker.md) of the inner loop.

The absence of the `correctness-checker` field means "do not perform correctness checking; accept the first output of [the edit loop](./edit-loop.md)".

The path is relative to the root of the Git repo.

## `edits-require-approval`

This field specifies a list of extra paths to files to which the harness will require human approval on every write (including creation or write).

Paths are relative to the source directory with which the harness is invoked - not relative to the Git repo root.

This field's absence is semantically equivalent to setting it to an empty list.

### Additional notes on required-approval files

There are some files which the harness considers out-of-the-box to be mutable only after explicit human approval for each edit, so these files do not need to be specified in `edits-require-approval`:

* `specs/*` (although the glob syntax is not recognised, so `edits-require-approval` cannot express this constraint anyway).

There are some files which the harness considers to be totally immutable (the human can't even approve edits to them), so should not be specified in `edits-require-approval` because the user cannot approve edits:

* the correctness checker executable, if configured. (Notwithstanding this protection, it is strongly recommended that users do not put the correctness checker inside the source directory with which the harness is invoked; doing so will greatly increase the attack surface if the LLM tries to manipulate the correctness checker.)

## `not-sent`

The contents of files in this section will not be sent to the LLM; instead, the LLM will receive the file path and the information that the contents have been omitted.

The harness rejects attempts to edit files in this list (because the LLM can't know what it's replacing).

Paths are relative to the source directory with which the harness is invoked - not relative to the Git repo root.
