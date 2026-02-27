kind: external-knowledge
id: external-knowledge/dockerfile
description: Some facts about Dockerfiles.
------

# `ENV` directive

The string specified in an `ENV` directive undergoes only minimal parsing.
From the docs:

The ENV instruction sets the environment variable `<key>` to the value `<value>`. This value will be in the environment for all subsequent instructions in the build stage and can be replaced inline in many as well. The value will be interpreted for other environment variables, so quote characters will be removed if they are not escaped. Like command line parsing, quotes and backslashes can be used to include spaces within values.

Example:

```
ENV MY_NAME="John Doe"
ENV MY_DOG=Rex\ The\ Dog
ENV MY_CAT=fluffy
```
