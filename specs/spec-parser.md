---
kind: spec
id: core/spec-format
description: Defines the format for user-generated specification documents.
---

Feature specs, stored in `specs/*.md`, are given as Markdown files with YAML frontmatter.

# The YAML frontmatter

## `kind`

This is exactly `spec`.

## `id`

A slash-delimited hierarchy of string identifiers locating this spec within some logical but unspecified tree, local to a single project.

## `description`

A human-readable (and LLM-readable) short description of what the spec defines.

# The spec body

After the frontmatter is a free-text Markdown document.

Humans write these (and LLMs can suggest new spec documents or edits to existing ones); LLMs read them when determining how a codebase should behave.
