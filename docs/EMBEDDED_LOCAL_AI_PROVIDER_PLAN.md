# Embedded Local AI Provider — Implementation Plan

## Goal

Add a bundled, CPU-capable local inference option to KubeDock without replacing or changing the existing API-provider and Ollama flows.

## Compatibility requirements

- Preserve Anthropic, Gemini, OpenAI, DeepSeek, Mistral, Groq, and Ollama provider configuration and request behavior.
- Preserve per-provider API model selection, custom model IDs, API keys, and task-profile model assignments.
- Keep provider selection explicit. Never silently fall back from local inference to a hosted provider, or from a hosted provider to local inference.
- Keep cloud-AI disablement enforcement and the existing redaction, bounded-response, read-only-by-default, and explicit-mutation-approval safeguards.
- Existing installations must retain their saved provider and model choices.

## User experience

- Add an independently selectable **Embedded Local Model** provider in AI settings.
- Retain model selection for every existing API provider. The existing editable model selector and custom-model management remain available.
- Offer a local model selector that lists only compatible, verified local model artifacts.
- Download the selected local model automatically on first use, not at application startup.
- Run downloads and inference outside the UI thread; expose progress, cancellation, retry, and actionable offline/error states.
- Do not require an API key for the embedded provider.

## Runtime and model constraints

- Bundle the inference engine with KubeDock installers for supported Windows, macOS, and Linux architectures. Users must not need to install Ollama, llama.cpp, Python packages, or another runtime separately.
- Keep model weights as the only separately downloaded runtime asset; enforce a maximum artifact size of 500 MB.
- Prefer a CPU baseline. GPU acceleration may be an optional platform-specific optimization and must not be required.
- Pin model artifact URL, license, expected byte size, and SHA-256 digest in reviewed application metadata. Reject oversized, truncated, mismatched, or unverified artifacts. Use a temporary file and atomic promotion after verification.
- Keep the model cache in the platform-appropriate application data directory and provide a clear recovery path for interrupted downloads.
- Do not claim hardware support or performance until measured on representative platform builds.

## Architecture work

1. Inspect the existing installer workflows and supported OS/architecture matrix before selecting the native runtime packaging strategy.
2. Add a provider adapter that conforms to the existing provider boundary and does not alter other provider request builders.
3. Implement a bounded model catalogue and downloader with checksum verification, cancellation, and atomic cache writes.
4. Implement lazy model initialization and inference in a managed worker; ensure one model load per selected artifact and safe shutdown.
5. Extend AI settings persistence additively, with migrations/defaults that preserve existing saved provider and API model selections.
6. Wire the embedded provider into all supported AI entry points, including explanation and conversation workers, while preserving the existing cloud policy.
7. Add tests for model metadata validation, download bounds/checksum failures, cancellation/retry, provider routing, no-cloud-fallback behavior, settings migration, and existing provider regressions.
8. Update installer workflows, documentation, resource requirements, and platform validation.

## Acceptance criteria

- Existing API providers, custom model IDs, and model selectors continue to work unchanged.
- A user can select an API provider and its exact API model independently of the embedded local model choice.
- A user can select the embedded provider and a supported local model without an API key.
- The model is downloaded only when first needed; no app-startup download occurs.
- No artifact larger than 500 MB is accepted, and no unverified model is loaded.
- CPU-only inference works on each declared supported platform.
- Local inference never triggers hosted requests, including when download or inference fails.
- Relevant unit and integration tests pass, and installer artifacts are validated on the supported OS/architecture matrix.

## Current validation status

This file records the implementation requirements and acceptance criteria only. It does not claim that the runtime, downloader, UI, installers, or tests have been implemented or validated.
