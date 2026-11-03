"""AI worker/provider access shared by KubeDock features."""

from __future__ import annotations

from .lifecycle import activate_lifecycle


class AIService:
    """Centralize AI worker construction and provider configuration access."""

    def __init__(self, commands=None):
        self.commands = commands

    def _register(self, worker):
        activate_lifecycle(self)
        return self.commands.register_worker(worker) if self.commands is not None else worker

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

    def create_model_test_worker(self, provider, api_key, model, parent=None):
        """Create a provider/model connectivity test and track it for shutdown."""
        import ai_assist
        return self._register(ai_assist.AIModelTestWorker(
            provider, api_key, model, parent,
        ))

    def create_command_explain_worker(self, command, exit_code, stderr, stdout):
        import ai_assist
        provider = ai_assist.get_provider()
        api_key = ai_assist.get_api_key(provider)
        if not api_key:
            raise RuntimeError("Configure an AI provider API key in Settings")
        return self._register(ai_assist.AICommandExplainWorker(
            provider, api_key, ai_assist.get_model(provider),
            command, exit_code, stderr, stdout,
        ))

    def create_explain_worker(self, pod, namespace, context, log_text):
        import ai_assist
        provider = ai_assist.get_provider()
        api_key = ai_assist.get_api_key(provider)
        if not api_key:
            raise RuntimeError("Configure an AI provider API key in Settings")
        return self._register(ai_assist.AIExplainWorker(
            provider, api_key, ai_assist.get_model(provider),
            pod, namespace, context, log_text,
        ))

    def create_kubernetes_interpret_worker(
        self, provider, api_key, model, request_text, namespace,
        context, history, *, conversation=None,
    ):
        from k8s_ai_ops import K8sAIInterpretWorker
        return self._register(K8sAIInterpretWorker(
            provider, api_key, model, request_text, namespace, context,
            history, conversation=conversation,
        ))

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
        ))
