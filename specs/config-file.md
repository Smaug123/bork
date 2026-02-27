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
    "edits-require-approval": ["flake.nix", "flake.lock"]
}
```

## `correctness-checker`

This field configures the location of the executable file which forms the [correctness checker](./correctness-checker.md) of the inner loop.

The absence of the `correctness-checker` field means "do not perform correctness checking; accept the first output of [the edit loop](./edit-loop.md)".

## `edits-require-approval`

This field specifies a list of extra paths to files (relative to the Git repository root) to which the harness will require human approval on every write (including creation or write).

This field's absence is semantically equivalent to setting it to an empty list.

The harness also considers some files to be mutable but only after explicit human approval for each edit:

* the correctness checker executable, if configured;
* `specs/*` (although the glob syntax is not recognised, so `edits-require-approval` cannot express this constraint anyway).

Note that the harness already considers some files to be totally immutable (which is stronger than the guarantee of `edits-require-approval`), so they also need not be specified in this list:

* the Bork config file itself (`.config/bork.json`);
* `.git/*`.

