---
kind: spec
id: core/edit-loop
description: Defines the algorithm a coding harness uses to invoke an LLM and bring a codebase in sync with a collection of specs.
---

The Bork system runs a reconciliation loop until convergence or until a cycle limit is reached.

The harness [is invoked](./harness-invocation.md) with a command line argument specifying the source directory to which all reads and writes are relative; the harness ensures that the agent does not escape this directory.

# Initial input format

Codebases are small enough that we can simply concatenate the entire codebase, along with every spec, and have the LLM determine divergences from the spec within a single context window.

The harness omits certain files:

* anything `.gitignore`'d
* any configured [correctness checker](./correctness-checker.md) (this is to help prevent the LLM from gaming the correctness checker)
* anything in `.config/bork.json`'s `"not-sent"` section (instead noting to the LLM that the file exists, but its contents are redacted).

(A current design assumption is that there are no nested Git directories.)

The coding harness does this concatenation using some reasonable mechanism to indicate the breaks between files, using filepaths to indicate what each file is, and it sends the request to the LLM to bring the codebase and specs into sync with each other.
The model is permitted to change the specs, but is strongly encouraged not to do so.

If the `specs/` folder is locally different from how it appears on the `main` branch (including new unstaged files), that diff is also supplied to the LLM, to highlight that this particular reconciliation is probably a "task to be performed/verified".
New unstaged files are not represented twice in the LLM input, but instead their filepath is indicated as being "newly added".

# Prompt

The actual prompt given to the LLM has the following properties:

* It encourages the model not to assume that any given piece of code is currently correct. (This is because the changes to the spec may be heavily divergent from the code, and the reconciliation loop must help them converge.)
* It emphasises that changes to the spec are a last resort and by default should only be performed if the specs themselves are contradictory (perhaps mutually contradictory). The direction of reconciliation should almost always be to change the code, not the specs.

# Intermediate output (e.g. tool calls)

The OpenAI tool calling API is used to make available the following tools.
In response to the tool call, the harness interactively transmits the LLM's request to the user, and receives a free text response which is fed back to the LLM to continue the conversation that makes up this iteration of the edit loop.

## resolve-spec-contradiction

When the LLM sees a contradiction within a spec or across several specs, it can tell us.
The LLM tells us which spec files are involved and gives us text snippets from those files, and identifies the nature of the contradiction.

## incomplete-spec

When the LLM finds that the specs are insufficient to take some decision, it can tell us.
The LLM tells us which spec files are involved (if any), and gives us descriptions of the nature of the incompleteness (the degrees of freedom still open).

# Final output format from one iteration

The LLM returns JSON of this format, where the keys of the `create-or-update` object indicate what files should exist:

```json
{
    "high-level-description": "A free-text description of changes performed, describing 'why' rather than 'what'.",
    "implementation-decisions": ["Free-text descriptions of any noteworthy decisions taken."],
    "create-or-update": {
        "foo/bar.py": {"rationale": "some rationale for an edit, or n/a if the change is routine", "contents": "import os\n..."}
    },
    "delete": [{"rationale": "rationale, or n/a if the change is routine", "file": "foo/baz.py"}],
}
```

Files in `specs` *may* be `create-or-update`d or even `delete`d, but the model is strongly encouraged not to do so unless it's incorporating the results of explicit tool calls to the user.

All paths are relative to the source directory command-line argument to the harness.

# Action taken in response to output

With a couple of exceptions, the coding harness simply replaces the files in `create-or-update` with the specified file contents, and deletes files which are specified in the `delete` list.

The exceptions are:

* any correctness checker configured in the `.config/bork.json` config file, which the harness never reads or writes, not even asking the user to approve changes;
* any attempts at filesystem traversal, including (for example) `../foo`, not even asking the user to approve changes;
* changes to `specs/`, which can be made but require individual human approval for each change;
* changes to any files configured in the configuration file's `edits-require-approval` list;
* changes to any files configured in the configuration file's `not-sent` list, which the harness doesn't ask the user to approve but instead silently discards.

The harness prevents symlink attacks when writing the files out.

If the LLM *does* try and edit a file which the harness refuses access to (like the correctness checker), the harness prints out the attempted contents.

# Commencing the next loop

Once the harness has written the output, it performs any correctness checks which may be specified, by running [the correctness checker](./correctness-checker.md) if it exists.
If any correctness checks fail, the harness commences a new loop, this time appending to the prompt the failing output in a format the LLM can consume.

The correctness checker is invoked in such a way that the user who is running the harness can see its stderr, and can supply stdin.
(This is so that a human reviewer can form part of the edit loop: the correctness checker is free to interactively ask the user for comments, for example.)

# Breaking out of the loop

If there are no findings from a correctness checker after a change is applied, the loop ends.
(Only loop once when there is no correctness checker.)

Alternatively, if five iterations take place and the model is still requesting changes, the harness applies those changes and then breaks out of the loop, requesting human intervention.
