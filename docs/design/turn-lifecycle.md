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
      -> automatic memory capture (deterministic, optional ambiguity-only LLM)
      -> resume capture journal stages (user model/state/episode/reflection/prospective)
      -> write capture completion marker
  <- TurnResult
```

## Invariants

1. Hosts do not run their own tool loop against model outputs.
2. Skill bodies loaded mid-turn should prefer tool_result scope.
3. Tool names not in callable set never execute.
4. Loop limit always enforced.
5. Every model exchange and tool call is traceable.
6. Automatic memory records cite the completed turn/tool evidence.
7. Optional capture failure is visible as a failed memory layer and does not
   rewrite the already determined task result.
8. Unknown extractor shapes or capture statuses are failures, never silent
   `skipped` results.
9. A completed task contributes `verified_check` goal evidence; Assistant text
   alone cannot complete authoritative Memory state.
10. Before capturing the new turn, automatic Memory resumes a bounded, fair
    batch of pending journal records for the active workspace from their
    durable prepared plans. An opaque StateStore identity fences L2 replay. A
    recovery failure is persisted and reported without changing the user-task
    result or starving other pending records.
11. On `submit_task_plan`, the Host binds `task_id → goal_id` (reuse existing
    current lifecycle goal when present; otherwise materialize
    `goal:<plan_turn_id>`). Terminal capture with a task id requires that
    binding.
12. Model-facing conversation state is assembled only through the model-safe
    State snapshot choke point (never raw Host-only fields such as
    `task_goal_bindings`).

## Closed-loop extension

Optional **task mode** wraps the model/tool loop with SQLite-persisted structured
steps and deterministic verification. Tool success alone does not mark a step
complete. Phases 14a–e add evidence-citing replan, effect-aware retry/approval,
opt-in evidence-bound L2 projection, a budgeted ContextCompiler, skill outcome
feedback, typed user state, and optional semantic/image checks. Required
tool/check evidence is verbatim and fails explicitly when it cannot fit.
See [agent-closed-loop.md](agent-closed-loop.md) and ROADMAP Phase 14.

## Failure mapping

| Failure | Result |
| --- | --- |
| Model provider error | `TurnResult.status=failed`, `ARIADNE_MODEL_ERROR` |
| Unknown tool | tool error result or turn fail per policy; default allow one recovery |
| Loop limit | `ARIADNE_TOOL_LOOP_LIMIT` |
| Memory required layer not ready | `ARIADNE_MEMORY_NOT_READY` if configured strict |
| Automatic capture protocol violation | failed `auto_capture` LayerReport with `ARIADNE_MEMORY_CAPTURE_PROTOCOL`; turn result unchanged |
| Pending capture recovery failure | failed `auto_capture` LayerReport with current-capture status and recovery counts in notes; turn result unchanged |
| Required prompt evidence over budget | `ARIADNE_CONTEXT_BUDGET_EXCEEDED` |
| Sandbox missing | `ARIADNE_SANDBOX_DISABLED` |
