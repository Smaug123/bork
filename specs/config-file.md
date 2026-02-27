---
kind: spec
id: core/config-file
description: Defines the user-written file that configures the Bork system.
---

There are a few knobs the user can turn in the Bork system; those knobs are configured in the `.config/bork.json` file at the root of the repository.

# JSON file format

```json
{
    "correctness-checker": "./correctness.py"
}
```

## `correctness-checker`

This field configures the location of the executable file which forms the [correctness checker](./correctness-checker.md) of the inner loop.
