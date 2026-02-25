kind: spec
id: core/edit-loop
description: Defines the algorithm a coding harness uses to invoke an LLM and bring a codebase in sync with a collection of specs.
------

Codebases are small enough that we can simply concatenate the entire codebase, along with every spec, and determine divergences from the spec.

The coding harness does this concatenation using some reasonable mechanism to indicate the breaks between files, and filepaths, and sends the request to the LLM to bring the codebase into compliance with the immutable specs.

The LLM returns JSON of this format, where the keys of the `create-or-update` object indicate what files should exist (*not* including the specs, which are immutable from the point of view of the LLM):

```json
{
    "create-or-update": {
        "foo/bar.py": "import os\n..."
    },
    "delete": ["foo/baz.py"]
}
```

The coding harness simply replaces the files in `create-or-update` with the specified file contents, and deletes files which are specified in the `delete` list; there is a carve-out for the `.git` directory, which the harness never touches.

The coding harness ensures that there are no file path traversals expressed by those keys and no symlink attacks when writing the files out.
