# KubeDock AI Enhancement Status

## Baseline inspection (2026-10-21)

Three highest-impact verified weaknesses were recorded before editing:

1. **External-context redaction was prompt-only.** `ai_assist.py` instructed the model not to disclose secrets but passed captured evidence without deterministic credential redaction.
2. **Pod diagnoses relied on the displayed logs.** `dialogs_parts/log_viewer.py` did not collect live pod status, events, configuration, or metrics before requesting a diagnosis.
3. **Mutation success was inferred from the initiating command.** `k8s_ai_ops_parts/operations.py` did not verify the resulting state before recording a mutation as successful.

## Implementation checklist

- [x] Trace provider request/response handling, AI explain/follow-up workers, Ops Mind parsing/validation, confirmation, execution, and history.
- [x] Collect target-specific pod description (status/readiness/configuration/events), the alternate current/previous container log stream when applicable, and container metrics through a bounded existing background worker. Secret contents are not queried.
- [x] Use a five-section evidence-grounded diagnosis format: Finding, Evidence, Likely cause and uncertainty, Recommended fix, and Verification.
- [x] Redact recognizable credentials, bearer/API tokens, private-key blocks, and credential-bearing command/output values before provider submission and from returned text/errors.
- [x] Bound provider request time, response/error body sizes and follow-up context. Retry once for transient connection failures or HTTP 5xx; do not retry rate-limit/authentication failures or read timeouts, and never switch providers.
- [x] Verify scale, restart, and delete outcomes with deterministic read-only probes; record `unverified` rather than success when target state cannot be confirmed.
- [x] Add focused regression tests and run them together with the existing suite in GitHub Actions.

## Test results

GitHub Actions on Python 3.12 (run `39646785124`, validating the alternate-log collection change):

- Focused tests: **10 passed**.
- Full test suite: **155 passed, 1 skipped**.
- Warnings: two pre-existing `SpeechRecognition` deprecations (`aifc` and `audioop`).

The final live-evidence refinement fetches the alternate log stream without re-reading the displayed stream; that version passed the same focused and full test suites.

## Known limitations

- Live diagnosis requires an active SSH/Kubernetes connection. `kubectl top` evidence depends on a functioning metrics API; unavailable sections are supplied as errors/unknowns rather than inferred.
- Evidence gathering is limited to one targeted pod description, one alternate log query (up to 100 lines), and one container-metrics query; it does not scan the cluster.
- Rollout verification is bounded to 30 seconds. A timeout, RBAC error, or unavailable connection reports `unverified` and requires a manual state check.
- Credential redaction is heuristic defense-in-depth and cannot guarantee detection of every organization-specific secret format. Kubernetes Secret values/private keys are not proactively queried for AI context.
- Transient retries are deliberately limited to one retry for connection failures and HTTP 5xx. Rate limits and timeouts are not retried to avoid unnecessary duplicate provider calls/costs.


## Post-merge follow-up (2026-10-21)

A quick review of the merged code found that resource evidence was tail-truncated to fit the prompt budget. For long pod descriptions this could drop the initial readiness/container state while retaining only later events. Added `_clip_resource_evidence()` to preserve both the beginning (status/configuration) and ending (events/metrics) within the same 4,000-character budget, with a marker showing omitted middle content. Added a regression test covering both ends. GitHub Actions validation passed: focused tests **11 passed**; full suite **156 passed, 1 skipped**. Two existing SpeechRecognition deprecation warnings remain.
