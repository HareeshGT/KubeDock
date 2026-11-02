"""AI worker/provider access shared by KubeDock features."""

from __future__ import annotations


class AIService:
    """Centralize AI worker construction and provider configuration access."""

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

    def create_command_explain_worker(self, command, exit_code, stderr, stdout):
        import ai_assist
        provider = ai_assist.get_provider()
        api_key = ai_assist.get_api_key(provider)
        if not api_key:
            raise RuntimeError("Configure an AI provider API key in Settings")
        return ai_assist.AICommandExplainWorker(
            provider, api_key, ai_assist.get_model(provider),
            command, exit_code, stderr, stdout,
        )
