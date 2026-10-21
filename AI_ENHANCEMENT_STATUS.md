# KubeDock AI Enhancement Status

## Baseline inspection (2026-10-21)

### Three highest-impact verified weaknesses
1. **External-context redaction is prompt-only, not deterministic.** `ai_assist.py` includes instructions about untrusted logs and secrets, but `_build_prompt` and `_build_command_prompt` pass captured text into prompts without removing credential-shaped values.
2. **Pod diagnoses rely on logs alone.** `dialogs_parts/log_viewer.py` forwards the displayed logs to `AIExplainWorker`; it does not first collect the pod's live status, readiness, specification, or recent events.
3. **Mutation success is inferred from the initiating command.** `k8s_ai_ops_parts/operations.py` records successful scale/restart/delete actions after a successful command exit, without verifying the resulting resource state.

### Implementation checklist
- [x] Trace provider request/response handling, AI explain/follow-up workers, Ops Mind parsing/validation, confirmation, execution, and history.
- [ ] P0: add compact, resource-specific live pod evidence to AI explanations while keeping collection off the GUI thread.
- [ ] P0/P1: deterministically redact sensitive values and cap provider response/error payloads.
- [ ] P0: verify mutating operation outcomes before marking them successful.
- [ ] Add focused tests for redaction, bounded provider response handling, and operation verification decisions.
- [ ] Run targeted tests and the existing regression suite; record exact outcomes.

### Baseline test status
Not run yet. This repository is being edited through GitHub API access; a local checkout/test runner is not available in this execution environment. CI will be checked after the changes are committed to the work branch.
