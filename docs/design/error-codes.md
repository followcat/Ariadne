# Design Note: Error Codes

Status: active  
Related: [../PUBLIC_API.md](../PUBLIC_API.md), [../DESIGN_PRINCIPLES.md](../DESIGN_PRINCIPLES.md)

## Convention

```text
ARIADNE_<AREA>_<DETAIL>
```

Stable enough for hosts to branch on.

## Initial set

| Code | Area | Meaning |
| --- | --- | --- |
| `ARIADNE_UNKNOWN_TOOL` | tools | call name not registered/callable |
| `ARIADNE_INVALID_TOOL_ARGS` | tools | schema validation failed |
| `ARIADNE_TOOL_HANDLER_ERROR` | tools | handler raised / returned failure |
| `ARIADNE_TOOL_DENIED` | tools | denied by host approval policy |
| `ARIADNE_TOOL_LOOP_LIMIT` | turn | exceeded max iterations |
| `ARIADNE_SKILL_NOT_FOUND` | skills | search/load target missing |
| `ARIADNE_SKILL_INVALID` | skills | pack failed validation |
| `ARIADNE_MEMORY_NOT_READY` | memory | required layer/projection incomplete |
| `ARIADNE_MEMORY_WRITE_FAILED` | memory | durable write failed |
| `ARIADNE_SANDBOX_DISABLED` | sandbox | exec without backend |
| `ARIADNE_SANDBOX_EXEC_FAILED` | sandbox | backend exec failure |
| `ARIADNE_MODEL_ERROR` | model | provider/API failure |
| `ARIADNE_PLUGIN_ERROR` | plugins | plugin config/call failure |
| `ARIADNE_CONFIG_INVALID` | bootstrap | bad construction config |

## Rules

1. Do not reuse codes with different meanings.
2. Prefer new codes over overloading `message` text.
3. `retriable` is explicit on `AppError`.
4. Infrastructure failures in eval harnesses should be separable from product assertion failures.
