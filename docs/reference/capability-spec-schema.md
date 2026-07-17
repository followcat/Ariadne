# Reference: CapabilitySpec Schema (v0)

```python
CapabilitySpec(
  name="memory",
  title="Memory",
  description="durable curated memory",  # catalog phrase
  tool_schema={
    "type": "function",
    "name": "memory",
    "description": "Detailed when/how/side-effects...",
    "parameters": {
      "type": "object",
      "properties": {...},
      "required": [...],
      "additionalProperties": False,
    },
    "strict": True,
  },
  tool_exposure="eager",  # eager | named_deferred | hidden
  exposed_to_llm=True,
)
```

## Rules

1. `description` is short.
2. Long policy goes in `tool_schema.description` or core policy.
3. Parameter descriptions are field-local.
4. Names are unique in the registry.
