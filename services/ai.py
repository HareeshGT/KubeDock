"""AI worker/provider access shared by KubeDock features."""

from __future__ import annotations

from .lifecycle import activate_lifecycle


class AIService:
    """Centralize AI worker construction and provider configuration access."""
    def stop(self):
        """Cancel background work owned by this feature service."""
        if self.commands is not None:
            self.commands.stop_service("ai")

    def __init__(self, commands=None):
        self.commands = commands

    def _register(self, worker, diagnostic_key="ai:worker"):
        activate_lifecycle(self)
        return (
            self.commands.register_worker(worker, diagnostic_key=diagnostic_key)
            if self.commands is not None else worker
        )

    @staticmethod
    def provider():
        import ai_assist
        return ai_assist.get_provider()

    @staticmethod
    def api_key(provider=None):
        import ai_assist
        return ai_assist.get_api_key(provider or ai_assist.get_provider())

    @staticmethod
    def model(provider=None):
        import ai_assist
        return ai_assist.get_model(provider or ai_assist.get_provider())

    @staticmethod
    def settings():
        import ai_assist
        return ai_assist.get_ai_settings()

    @staticmethod
    def task_provider_selection(task, fallback_provider, fallback_key, fallback_model):
        import ai_assist
        return ai_assist._task_provider_selection(
            task, fallback_provider, fallback_key, fallback_model,
        )

    def create_agent_registry(self, context=""):
        """Build an explicit registry of tools backed by existing shared services."""
        from ai_agent import ToolRegistry
        from ai_agent.integrations.kubernetes import register_kubernetes_read_tools
        if self.commands is None:
            raise RuntimeError("The shared command service is unavailable.")
        from .kubernetes import KubernetesService
        kubernetes = KubernetesService(self.commands.connection, self.commands)
        registry = ToolRegistry()
        register_kubernetes_read_tools(registry, kubernetes, context=context)
        return registry

    def create_agent_runtime(self, registry=None, policy=None, context=""):
        """Create the bounded agent engine with explicitly registered capabilities."""
        from ai_agent import AgentPolicy, AgentRuntime
        registry = registry if registry is not None else self.create_agent_registry(context=context)
        return AgentRuntime(registry, policy or AgentPolicy())

    def create_agent_adapter(self, provider=None, api_key=None, model=None, timeout_seconds=90):
        """Create a structured tool-call adapter using KubeDock's existing provider boundary.

        The adapter deliberately uses a provider-neutral JSON tool-call contract
        for every currently supported provider. Native function-calling adapters
        can replace it later without changing the registry or runtime.
        """
        import ai_assist
        from ai_agent import PromptToolAdapter
        fallback_provider = provider or self.provider()
        fallback_key = self.api_key(fallback_provider) if api_key is None else api_key
        fallback_model = model or self.model(fallback_provider)
        selected_provider, selected_key, selected_model, error = self.task_provider_selection(
            "quick", fallback_provider, fallback_key, fallback_model,
        )
        if error:
            raise RuntimeError(error)
        if selected_provider != "ollama" and not selected_key:
            raise RuntimeError("Configure an AI provider API key in Settings.")
        return PromptToolAdapter(
            selected_provider, selected_key, selected_model,
            ai_assist._call_provider, timeout_seconds=timeout_seconds,
        )

    def create_agent(self, registry=None, policy=None, provider=None, api_key=None, model=None, context=""):
        """Return a ready agent with bounded read-only Kubernetes tools by default."""
        runtime = self.create_agent_runtime(registry, policy, context=context)
        adapter = self.create_agent_adapter(provider, api_key, model)
        return runtime, adapter

    def create_model_test_worker(self, provider, api_key, model, parent=None):
        """Create a provider/model connectivity test and track it for shutdown."""
        import ai_assist
        return self._register(ai_assist.AIModelTestWorker(
            provider, api_key, model, parent,
        ), "ai:model-test")

    def create_command_explain_worker(self, command, exit_code, stderr, stdout):
        import ai_assist
        provider = ai_assist.get_provider()
        api_key = ai_assist.get_api_key(provider)
        if not api_key:
            raise RuntimeError("Configure an AI provider API key in Settings")
        return self._register(ai_assist.AICommandExplainWorker(
            provider, api_key, ai_assist.get_model(provider),
            command, exit_code, stderr, stdout,
        ), "ai:command-explain")

    def create_explain_worker(self, pod, namespace, context, log_text):
        import ai_assist
        provider = ai_assist.get_provider()
        api_key = ai_assist.get_api_key(provider)
        if not api_key:
            raise RuntimeError("Configure an AI provider API key in Settings")
        return self._register(ai_assist.AIExplainWorker(
            provider, api_key, ai_assist.get_model(provider),
            pod, namespace, context, log_text,
        ), "ai:pod-explain")

    def create_kubernetes_interpret_worker(
        self, provider, api_key, model, request_text, namespace,
        context, history, *, conversation=None,
    ):
        from k8s_ai_ops import K8sAIInterpretWorker
        return self._register(K8sAIInterpretWorker(
            provider, api_key, model, request_text, namespace, context,
            history, conversation=conversation,
        ), "ai:kubernetes-interpret")

    def create_investigation_worker(
        self, provider, api_key, model, question, target, evidence,
        *, prometheus_url="", history_days=7, parent=None,
    ):
        from ops_mind_investigation import OpsMindInvestigationWorker
        return self._register(OpsMindInvestigationWorker(
            provider, api_key, model, question, target, evidence,
            prometheus_url=prometheus_url,
            history_days=history_days,
            parent=parent,
        ), "ai:investigation")
