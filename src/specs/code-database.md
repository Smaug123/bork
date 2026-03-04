---
kind: spec
id: core/code-database
description: Defines the format and location of the deterministically-maintained database that will eventually permit the Bork system to filter relevant context files, as well as how the database is constructed.
---

In order to scale to codebases which are larger than will fit in the context of an LLM, Bork can read a simple database which summarises code.
It doesn't use this database anywhere, but future extensions to Bork will permit using this database to identify where and how to make changes to code.

This database lives outside the code, so is not directly accessible to the LLM through the harness.

The harness is simply a JSON file listing all the relevant definitions in the code.
Here is a representative example, although it is not prescriptive: the system actually stores whatever can most conveniently be extracted from the code deterministically.

```json
{
    "foo/file.rs": [
      ["some_method_name", "function -> signature", "docstring, or JSON null"],
      ["some_trait_name", "", "docstring, or JSON null"]
    ],
    "bar/file.py": [
      ["SomeClassName", "inheritance data", "docstring, or JSON null"],
      ["SomeClassName.__init__", "def __init__(self) -> SomeClassName", "docstring, or JSON null"]
    ]
}
```

The function signature (or inheritance data, or whatever analogous concept) is free text, defined however the relevant language ecosystem makes it convenient to obtain deterministically.

# Constructing the database

The Bork system contains a standalone tool which can deterministically construct the database.
It uses tree-sitter to parse each file in the codebase and render the necessary information from it.

The following languages are implemented for ingestion into the database:

* Python.

Files written in the following languages are ignored when constructing the database:

* Markdown.

The Bork harness quits if an unrecognised language is encountered.
