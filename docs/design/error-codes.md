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
| `ARIADNE_MEMORY_CAPTURE_PROTOCOL` | memory | automatic extractor/status violated its closed protocol |
| `ARIADNE_MEMORY_CAPTURE_JOURNAL_INVALID` | memory | capture journal schema, stage, or reference is invalid |
| `ARIADNE_MEMORY_CAPTURE_CONFLICT` | memory | one turn id was replayed with different capturable evidence |
| `ARIADNE_MEMORY_CAPTURE_CAPACITY` | memory | capture journal hard cap exceeded |
| `ARIADNE_MEMORY_CAPTURE_AFFINITY` | memory | pending capture workspace or StateStore identity does not match the active projector |
| `ARIADNE_MEMORY_CAPTURE_RESUME_FAILED` | memory | a pending capture stage raised outside a structured Ariadne error during bounded recovery |
| `ARIADNE_MEMORY_EVIDENCE_BUDGET` | memory | one evidence unit cannot fit its serialized-byte hard cap |
| `ARIADNE_EPISODE_INVALID` | memory | episode schema/event/evidence is invalid |
| `ARIADNE_EPISODE_CAPACITY` | memory | episode or event hard cap exceeded |
| `ARIADNE_EPISODE_NOT_FOUND` | memory | requested Episode is absent or outside the active scope |
| `ARIADNE_EPISODE_EVENT_NOT_FOUND` | memory | evidence cursor does not name a visible event |
| `ARIADNE_REFLECTION_INVALID` | memory | reflection threshold/action/schema is invalid |
| `ARIADNE_REFLECTION_NOT_FOUND` | memory | requested reflection candidate does not exist |
| `ARIADNE_REFLECTION_CONFIRMATION_REQUIRED` | memory | candidate/action/session confirmation contract is missing or mismatched |
| `ARIADNE_PROSPECTIVE_INVALID` | memory | prospective trigger/action/schema is invalid |
| `ARIADNE_PROSPECTIVE_NOT_FOUND` | memory | requested prospective reminder does not exist |
| `ARIADNE_PROSPECTIVE_CAPACITY` | memory | prospective reminder hard cap exceeded |
| `ARIADNE_SANDBOX_DISABLED` | sandbox | exec without backend |
| `ARIADNE_SANDBOX_EXEC_FAILED` | sandbox | backend exec failure |
| `ARIADNE_MODEL_ERROR` | model | provider/API failure |
| `ARIADNE_PLUGIN_ERROR` | plugins | plugin config/call failure |
| `ARIADNE_CONFIG_INVALID` | bootstrap | bad construction config |
| `ARIADNE_MULTIMODAL_UNSUPPORTED` | model/host | images attached but model/vision policy rejects multimodal |
| `ARIADNE_TASK_SCHEMA_MIGRATION_REQUIRED` | tasks | known legacy TaskState needs explicit operator handling |
| `ARIADNE_TASK_SCHEMA_UNSUPPORTED` | tasks | TaskState schema version is unknown |
| `ARIADNE_TASK_AUDIT_MISMATCH` | tasks | current snapshot and revision event chain disagree |

## Rules

1. Do not reuse codes with different meanings.
2. Prefer new codes over overloading `message` text.
3. `retriable` is explicit on `AppError`.
4. Infrastructure failures in eval harnesses should be separable from product assertion failures.
