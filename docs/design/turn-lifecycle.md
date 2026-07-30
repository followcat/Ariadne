# Design Note: Turn Lifecycle

Status: active  
Related: [../ARCHITECTURE.md](../ARCHITECTURE.md), [../PUBLIC_API.md](../PUBLIC_API.md)

## Sequence

```text
Host
  -> TurnApplication.run(command)
      -> MemoryFacade.build_context
      -> SkillSelector.plan
      -> ToolRegistry.build_exposure
      -> ContextCompiler.compile (ordered blocks + attribution)
      -> loop:
           ModelPort.stream/complete
           if tool_calls:
             validate + ToolRuntime.invoke
             append results
           else:
             break
      -> persist traces / schedule memory writes
  <- TurnResult
```

## Invariants

1. Hosts do not run their own tool loop against model outputs.
2. Skill bodies loaded mid-turn should prefer tool_result scope.
3. Tool names not in callable set never execute.
4. Loop limit always enforced.
5. Every model exchange and tool call is traceable.

## Closed-loop extension

Optional **task mode** wraps the model/tool loop with SQLite-persisted structured
steps and deterministic verification. Tool success alone does not mark a step
complete. Phases 14a–c add evidence-citing replan, effect-aware retry/approval,
opt-in evidence-bound L2 projection, and a budgeted ContextCompiler. Required
tool/check evidence is verbatim and fails explicitly when it cannot fit.
See [agent-closed-loop.md](agent-closed-loop.md) and ROADMAP Phase 14.

## Failure mapping

| Failure | Result |
| --- | --- |
| Model provider error | `TurnResult.status=failed`, `ARIADNE_MODEL_ERROR` |
| Unknown tool | tool error result or turn fail per policy; default allow one recovery |
| Loop limit | `ARIADNE_TOOL_LOOP_LIMIT` |
| Memory required layer not ready | `ARIADNE_MEMORY_NOT_READY` if configured strict |
| Required prompt evidence over budget | `ARIADNE_CONTEXT_BUDGET_EXCEEDED` |
| Sandbox missing | `ARIADNE_SANDBOX_DISABLED` |
