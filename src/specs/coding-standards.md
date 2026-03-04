---
kind: spec
id: core/coding-standards
description: Defines the coding standards used when writing the code that makes up Bork.
---

# Type-checking

The type-checker is a powerful tool to add guarantees to program correctness.
We use it to its fullest extent, defining types to model the domain correctly.
We don't suppress any part of the type checker unless it is *absolutely necessary*.

# Comments

Every code entity (function, class, etc) has a docstring describing what it does and what features it's relevant for, as well as (if necessary) any important restrictions on how to use it (e.g. a statement of the semantics of a string argument to a function).

These docstrings are important because the [code database](./code-database.md) consumes them; this will eventually be used to help the Bork system know how to work on its own codebase.
