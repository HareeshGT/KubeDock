# KubeDock AI Intelligence Workspace

Branch: `feat/ai-infrastructure-intelligence`
Pull request: https://github.com/HareeshGT/KubeDock/pull/41

## Implemented

- Independent provider and model selection for Quick, Deep Diagnosis, Voice/Conversation, and Local/Private task profiles in Settings → AI.
- Optional escalation from quick command explanations to the configured Deep Diagnosis model when the command fails or the evidence is large.
- Ollama local inference using the fixed loopback endpoint `http://127.0.0.1:11434/v1/chat/completions`, with no API key and no silent cloud fallback.
- Explicit Analyze Locally action from the pod log viewer.
- Redacted incident bundle export with timestamp, target metadata, collected logs/resource evidence, candidate signals, and SHA-256 digest.
- Incident Investigator tab to analyze an explicitly selected bundle with the configured Deep Diagnosis profile.
- Deterministic candidate signal correlation for OOM, probe failures, image pull failures, scheduling constraints, dependency networking/DNS, and permission failures.
- AI Intelligence Workspace tabs for:
  - Live read-only capture and comparison of two kubectl contexts.
  - Imported JSON snapshot comparison.
  - Explicit, finite, read-only resource watch over the existing SSH session.
  - Conservative right-sizing recommendations from user-supplied usage and sample count.
  - Remediation proposal validation with an action allowlist, protected namespaces, explicit delete confirmation, human approval requirement, and required verification.
  - Live searchable service discovery over SSH or imported service inventory.
  - Cross-system signal correlation from supplied host, Docker, application, Kubernetes, and metrics evidence.
  - Opt-in local aggregate usage/error/latency counters.
- AI regression CI compiles the new modules, runs AI-focused tests, and runs the full pytest suite.
- README and AI enhancement status documentation updated.

## Safety invariants

- The workspace never executes a remediation plan. Existing Ops Mind confirmation, RBAC, protected namespace checks, and post-mutation verification remain required.
- Watch mode requires an explicit start, caps duration at one hour, validates polling intervals, and performs only read-only `kubectl get` requests.
- Cluster inventory capture omits Secret values and normalizes only resource identity, phase, replica state, images, ports, and restart counts.
- Incident evidence is redacted before export and again before AI provider submission. Captured evidence is untrusted data and is not treated as instructions.
- Ollama is called only at loopback; a local failure is shown to the user rather than switching to a cloud provider.
- Usage telemetry is disabled by default and records provider/profile, call count, error count, and aggregate latency only. It does not store prompts, logs, responses, or API keys.

## Validation

- AI-focused regression tests: passed in GitHub Actions.
- Full pytest suite on Python 3.12: latest confirmed successful run reported **217 passed, 2 skipped, 2 warnings, 7 subtests passed** on the latest successful run; re-run after any subsequent commit before merging.
- Full module compilation: passed in GitHub Actions.
- Live cluster behavior, real provider connectivity, Ollama installation/model availability, voice hardware, and packaged application smoke tests require validation on the target environment.

## Known limitations

- Right-sizing can collect five live usage samples with `kubectl top` and read current pod requests over SSH. It remains advisory, does not query historical Prometheus windows, and never writes resource requests.
- The watch tab polls a single named resource and correlates its observations; it does not automatically remediate or continuously scan the cluster.
- Cross-system correlation is deterministic signal matching over evidence supplied by the user or collected for an incident; it does not prove causality.
- Multi-cluster comparison is based on selected kubectl contexts available through the current SSH session, or user-provided JSON snapshots. It does not create or configure cluster credentials.
- Service navigation discovers and filters service records and displays a safe read-only inspection command; it does not expose a service or automatically change cluster state.
