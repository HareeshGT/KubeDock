# AI Intelligence Implementation — Branch Scope

Branch: feat/ai-infrastructure-intelligence

## First implementation slice

This branch introduces a deterministic, dependency-free task-profile resolver and unit tests for the routing rules. It is a foundation only: the resolver does not itself modify provider settings or dispatch AI requests, and it must be wired into the existing Settings / AI request path before task-aware routing is user-visible.

## Invariants

- No model is selected based on a guessed capability inferred from its display name.
- Missing or incomplete assignments raise an explicit error instead of silently substituting another model.
- Automatic escalation is disabled unless the caller explicitly enables it and marks the task as complex.
- A local/private profile accepts only explicitly configured local or compatible endpoint identifiers.
- A single-model override is explicit and observable in the returned reason.

## Validation

Run: python -m unittest discover -s tests -p 'test_ai_task_profiles.py' -v

## Remaining work from the full enhancement specification

This branch does not yet complete the full enhancement program. Before merging as a complete implementation, the remaining phases must be integrated and validated against the actual UI and infrastructure paths: secret-flow regression coverage; incident evidence collection and report UI; safe remediation plan integration; profile persistence/UI/provider dispatch; telemetry; cross-system diagnosis; scoped watch mode; multi-cluster comparison; right-sizing recommendations; voice lifecycle improvements; service navigation; comprehensive CI and updated AI_ENHANCEMENT_STATUS.md.

No real-cluster, API-key, hardware, or packaging validation is claimed by this foundation slice.
