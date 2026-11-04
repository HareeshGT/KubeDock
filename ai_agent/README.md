# KubeDock AI Agent Runtime

This package is the first implementation layer for evolving Ops Mind from a
single-operation interpreter into a bounded, multi-step agent.

## Architecture

- `ToolRegistry` stores application-owned functions and JSON argument schemas.
  Model output can select registered names only; it cannot supply Python or
  arbitrary shell code.
- `AgentRuntime` runs a bounded reason/tool/result loop and returns structured
  events for a UI to display.
- `AgentPolicy` limits iterations, tool calls, execution time, and result size.
  It can also restrict the allowed tool names.
- Read-only tools may run automatically. A tool marked `permission="write"`
  is not executed unless an explicit approval callback approves that exact
  tool and validated argument set. Without approval, the run returns
  `needs_approval`.
- `PromptToolAdapter` bridges the existing `ai_assist._call_provider`
  boundary using a strict JSON tool-call envelope. This keeps the runtime
  independent of vendor APIs while native tool-call adapters are added later.
- `AIService.create_agent()` provides the shared service entry point and
  respects the existing task-provider selection and hosted/local policy.

## Adapter contract

A provider adapter implements:

```python
complete(messages=..., tools=...) -> AssistantMessage
```

It receives normalized conversation messages and tool schemas, and returns
plain assistant text or one or more normalized `ToolCall` values. The runtime
owns authorization and execution; provider adapters never execute tools.

## Current integration boundary

This commit adds the runtime, provider-neutral prompt adapter, shared AIService
factory, and five initial read-only Kubernetes tools: list pods, list
deployments, list nodes, read bounded namespace events, and read a bounded pod
log tail. They execute through KubeDock's existing shared command/connection
service. Namespace, context, pod name, and limits are validated before a
command is built; log output is redacted before it returns to the model.

Existing Ops Mind and Kubernetes operations remain unchanged and continue to
use their existing validation, confirmation, RBAC, and verification paths.
No mutation tool is registered. Explicit mutation requests remain on the
existing Ops Mind path and retain its confirmation, protected-namespace, RBAC,
and verification checks. Read-only Ops Mind requests now use the multi-step
agent and show a compact tool timeline. Hosted-provider diagnostic requests
ask before enabling the pod-log tool; declining leaves metadata, node, and
event tools available but does not expose logs to the agent. Local Ollama can
use the bounded log tool without sending logs to a hosted provider. Docker,
SSH host inspection, and monitoring tools remain follow-up integrations.

## Test

Run the focused tests with:

```bash
python -m unittest discover -s tests -p 'test_ai_agent_*.py'
```
