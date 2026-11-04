# Embedded Local AI Provider — Implementation Plan

## Goal

Add an in-process, CPU-capable Python GGUF inference option to KubeDock without replacing or changing the existing API-provider and Ollama flows.

## Compatibility requirements

- Preserve Anthropic, Gemini, OpenAI, DeepSeek, Mistral, Groq, and Ollama provider configuration and request behavior.
- Preserve per-provider API model selection, custom model IDs, API keys, and task-profile model assignments.
- Keep provider selection explicit. Never silently fall back from local inference to a hosted provider, or from a hosted provider to local inference.
- Keep cloud-AI disablement enforcement and the existing redaction, bounded-response, read-only-by-default, and explicit-mutation-approval safeguards.
- Existing installations must retain their saved provider and model choices.

## User experience

- Add an independently selectable **Embedded Local AI (Python / CPU)** provider in AI settings.
- New installations default to Embedded Local AI when no provider or API key has been configured; existing saved provider selections are preserved.
- Retain model selection for every existing API provider. The existing editable model selector and custom-model management remain available.
- Offer a local model selector that lists only compatible, verified local model artifacts.
- Offer an explicit model download during installation. If skipped, the user can rerun the installer to add the model. Do not attempt to write model weights into a protected application directory from the running app.
- Run downloads and inference outside the UI thread; expose progress, cancellation, retry, and actionable offline/error states.
- Do not require an API key for the embedded provider.

## Runtime and model constraints

- Bundle `llama-cpp-python` and its native libraries into KubeDock release/source-build artifacts for supported Windows, macOS, and Linux architectures. Users must not need to install Ollama, llama.cpp, Python packages, or another runtime separately.
- Keep model weights as the only separately downloaded runtime asset; enforce a maximum artifact size of 500 MB. When selected during installation, install them directly into the KubeDock application bundle. A skipped model is installed by rerunning the installer; runtime self-updates must not write into protected app directories.
- Prefer a CPU baseline. GPU acceleration may be an optional platform-specific optimization and must not be required.
- Pin model artifact URL, license, expected byte size, and SHA-256 digest in reviewed application metadata. Reject oversized, truncated, mismatched, or unverified artifacts. Use a temporary file and atomic promotion after verification.
- Store model weights inside the installed KubeDock application resources (`models/`; macOS: `Contents/Resources/models/`), never in a separate user-data/cache directory. Downloads are staged in a temporary directory, verified, and then copied into the app bundle by the installer. The app itself must not silently write to protected install locations.
- Do not claim hardware support or performance until measured on representative platform builds.

## Architecture work

1. Inspect the existing installer workflows and supported OS/architecture matrix before selecting the native runtime packaging strategy.
2. Add a provider adapter that invokes `llama-cpp-python` directly in the KubeDock worker process and does not alter other provider request builders.
3. Implement a bounded model catalogue and downloader with checksum verification, cancellation, and atomic installation into the app's `models/` resources directory; fail with a clear installer/admin instruction if the bundle is not writable.
4. Implement lazy model initialization and inference in a managed worker; resolve weights from the app bundle, ensure one model load per selected artifact, and shut down safely.
5. Extend AI settings persistence additively, with migrations/defaults that preserve existing saved provider and API model selections.
6. Wire the embedded provider into all supported AI entry points, including explanation and conversation workers, while preserving the existing cloud policy.
7. Add tests for model metadata validation, download bounds/checksum failures, cancellation/retry, provider routing, no-cloud-fallback behavior, settings migration, and existing provider regressions.
8. Update installer workflows, documentation, resource requirements, and platform validation.

## Acceptance criteria

- Existing API providers, custom model IDs, and model selectors continue to work unchanged.
- A user can select an API provider and its exact API model independently of the embedded local model choice.
- A user can select the embedded provider and a supported local model without an API key.
- The model is either included in the installer/app bundle or explicitly downloaded during installation; no app-startup download occurs.
- No artifact larger than 500 MB is accepted, and no unverified model is loaded.
- CPU-only inference works on each declared supported platform.
- Local inference never triggers hosted requests, including when download or inference fails.
- Relevant unit and integration tests pass, and installer artifacts are validated on the supported OS/architecture matrix.

## Current validation status

The initial Python provider integration and PyInstaller collection wiring are implemented on the feature branch. Cross-platform native-wheel resolution, release builds, runtime inference, installer flows, and automated test execution still require validation; this plan does not claim those checks have passed.
