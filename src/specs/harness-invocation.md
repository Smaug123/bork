---
kind: spec
id: core/harness-invocation
description: Defines the interface the user uses to invoke the edit loop.
---

The [edit loop](./edit-loop.md) describes the Bork harness which constructs an agent to operate on a codebase.

# Invocation

The user invokes this harness by supplying the following positional arguments on the command line:

* a source directory, within which the agent is constrained to read and write;
* an optional [code database file](./code-database.md) to read from and write to (although neither reading nor writing from the harness is currently implemented).

The harness is executable on Unix.
