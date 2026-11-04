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
factory, and read-only Kubernetes tools for listing pods, deployments and nodes,
reading namespace events and bounded pod logs, and retrieving one exact key
from one explicitly named ConfigMap. The ConfigMap tool returns only the
requested key/value, never the entire object. They execute through KubeDock's existing shared command/connection
service. Namespace, context, pod name, and limits are validated before a
command is built; log output is redacted before it returns to the model.

Existing Ops Mind workflows remain in place for mutations and read operations
that require capabilities not yet exposed to the agent. Mutation requests, resource describe requests, Secret key reads, and
investigations continue through the established interpreter and its existing
confirmation, protected-namespace, RBAC, evidence-consent, and verification
paths. Exact ConfigMap key reads may use the agent's scoped read-only tool.
The agent policy requires evidence for live facts, carries tool execution
status into model context, and treats failed/forbidden/missing reads as
unverified. Conversation history cannot replace the mandatory system policy.
For an exact ConfigMap key lookup, the runtime constructs the final answer
from the successful exact-object tool result instead of asking the model to
restate the value. Unsupported sources or failed lookups produce a safe
clarification rather than a guessed value. Supported read-only inventory and
diagnostic requests use the multi-step agent and show a compact tool timeline. Hosted-provider diagnostic requests ask before
enabling the bounded pod-log tool. If that agent-specific consent is declined,
the request falls back to the established Ops Mind interpreter, which applies
its own evidence-sharing consent before collecting investigation logs. Local
Ollama can use the bounded log tool without sending logs to a hosted provider.
Docker, SSH host inspection, and monitoring tools remain follow-up integrations.

## Test

Run the focused tests with:

```bash
python -m unittest discover -s tests -p 'test_ai_agent_*.py'
```
