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

This commit adds the runtime, provider-neutral prompt adapter, tests, and the
shared AIService factory. Existing Ops Mind and Kubernetes operations remain
unchanged and continue to use their existing validation, confirmation, RBAC,
and verification paths. No Kubernetes, Docker, SSH, or monitoring tool is
registered automatically yet. Those capabilities must be exposed through
small, bounded service methods and registered explicitly in a follow-up
integration step; existing UI-bound workers and mutation safeguards must not
be bypassed.

## Test

Run the focused tests with:

```bash
python -m unittest discover -s tests -p 'test_ai_agent_*.py'
```
