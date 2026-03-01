---
kind: spec
id: core/harness-invocation
description: Defines the interface the user uses to invoke the edit loop.
---

The [edit loop](./edit-loop.md) describes the Bork harness which constructs an agent to operate on a codebase.

# Invocation

The user invokes this harness by supplying a single positional argument on the command line: a source directory.
(The agent is constrained to read and write within this source directory.)

