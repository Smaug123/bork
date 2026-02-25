kind: external-knowledge
id: external-knowledge/openai-api
description: Some facts about the OpenAI Python client library and API.
------

The default reasoning effort for GPT-5.2 is None.

The Responses API has reasoning effort specified by:

```python
response = client.responses.create(
    model="gpt-5.2",
    input="How much gold would it take to coat the Statue of Liberty in a 1mm layer?",
    reasoning={
        "effort": "none"
    }
)
```

The Completions API has reasoning effort specified by:

```python
response = client.chat.completions.create(
    model="gpt-5.2",
    messages=[{"role": "user", "content": "How much gold would it take to coat the Statue of Liberty in a 1mm layer?"}],
    reasoning_effort="none"
)
```
