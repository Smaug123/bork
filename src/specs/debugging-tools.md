---
kind: spec
id: core/debugging-tools
description: Defines how the user of Bork can inspect what's going on in a Bork run.
---

The Bork system accepts an environment variable `BORK_ENABLE_DEBUG_LOG=1`, which (when set):

* prints to stderr the requests to and responses from the LLM in the [edit loop](./edit-loop.md).
