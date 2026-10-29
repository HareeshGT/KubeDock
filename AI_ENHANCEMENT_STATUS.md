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


## Ops Mind conversational voice upgrade (2026-10-21)

### Verified implementation
- [x] Add JARVIS, FRIDAY, and EDITH personality profiles with distinct local response wording, spoken status phrasing, default speaking pace, and per-personality system voice settings.
- [x] Persist personality, selected voice IDs, hands-free preference, silence threshold, speech pace, and volume using the existing SQLite-backed settings API.
- [x] Add explicit voice states and a configurable local audio-level silence detector; retain push-to-talk as an alternative and bound no-speech/utterance duration.
- [x] Add hands-free re-listening after each completed turn, transcript duplicate suppression, and an “Interrupt & Listen” control. Interruption does not cancel a Kubernetes operation already in progress; its request is queued and still goes through the existing validation and confirmation path.
- [x] Make local greetings, short status updates, clarifications, and operation outcomes more natural for the selected personality. Sanitize code blocks, Markdown, raw JSON, and stack traces before local speech.
- [x] Maintain a short, in-memory, redacted conversation transcript so prior outcomes can help answer follow-ups. Fresh-state questions continue through live Kubernetes operations instead of relying on stale conversation text.
- [x] Support a typed, bounded conversational AI response for questions/follow-ups that do not require a new operation or live state.
- [x] Support a bounded, read-only logs action for current or previous pod logs, with validated pod/container/namespace fields. Log output is displayed in the UI but omitted from persistent Ops Mind history; a short redacted excerpt may be held in session memory.
- [x] Preserve provider handling, allowlisted operations, resource validation, protected namespaces, and existing confirmation safeguards.

### Speech setup and behavior
- Speech-to-text: existing SpeechRecognition + PyAudio capture with Google Web Speech transcription. Hands-free end-of-utterance detection is local; captured speech is sent to Google for transcription. Raw audio recordings are not saved by KubeDock.
- Text-to-speech: existing optional local pyttsx3 engine and the system voices available on the machine. No external voice API or audio samples are added. The persona selector changes delivery/style, system voice preference, and speaking pace; these are original profiles, not cloned Marvel/actor voices.
- Optional voice dependencies remain in requirements-voice.txt; the main application can still run without them. Voice-related controls fall back to disabled/unavailable states when speech dependencies or a microphone are absent.
- No streaming partial transcription is available from the current Google recognizer call. Playback interruption is exposed through the explicit “Interrupt & Listen” control; fully automatic acoustic barge-in detection while the assistant is speaking is not implemented because the microphone is not concurrently monitored during TTS playback.
- Voice capture, ASR, and TTS run in existing background worker/thread paths. TTS interruption is best-effort because OS speech drivers vary.
- No continuous background AI scans are started; the microphone is active only while a voice utterance is being captured.

### Voice regression tests
- Added tests/test_ops_mind_voice.py for voice-state transitions, VAD thresholds, duplicate transcript prevention, TTS sanitization, personality profiles/responses, bounded conversational context, and safe log command construction.
- GitHub Actions validation on Python 3.12: focused voice/security/ops tests **27 passed**; full suite **172 passed, 1 skipped**. Two pre-existing SpeechRecognition deprecation warnings (`aifc`, `audioop`) remain. Run: https://github.com/HareeshGT/KubeDock/actions/runs/39654258687


## Neural JARVIS / FRIDAY / EDITH-inspired voice profiles (2026-10-22)

- [x] Added an optional OpenAI expressive text-to-speech backend using gpt-4o-mini-tts at POST /v1/audio/speech. It uses built-in Cedar, Coral, and Sage voices, explicit speed control, and separate delivery instructions for the three original character-inspired profiles.
- [x] Added a Neural · OpenAI / System · Offline engine selector. Neural output reuses the OpenAI API key saved under Settings → AI; the offline pyttsx3 engine remains available as a fallback.
- [x] Applied persona-specific tone, delivery, pace and phrasing to spoken status updates and voice previews. The profiles are inspired by cinematic AI assistants, not direct copies of any actor or exact copyrighted character voice.
- [x] Neural TTS runs in a background QThread. The WAV response is bounded in memory, played through Qt Multimedia, and written to a temporary file with restrictive permissions that is deleted after playback.
- [x] Added fallback to the local system voice if the HTTP API or playback fails; transient failures do not permanently change the selected engine. Tests mock the HTTP response and WAV payload, requiring neither real API credentials nor audio hardware.
- [x] Documented neural voice transport and the offline fallback in requirements.txt.

### Neural voice setup and costs
- Select Neural · OpenAI in Ops Mind, choose JARVIS, FRIDAY, or EDITH, then use the preview button. The OpenAI key must be present under Settings → AI.
- OpenAI receives the text to be spoken, and neural audio generation can incur API usage charges. No additional voice-specific API key is required. The microphone transcription path is unchanged and still sends captured speech to Google Web Speech.
- If neural dependencies are missing or an OpenAI request fails, Ops Mind switches to the offline system voice. Voice quality varies by generated voice and provider delivery; the profiles are character-inspired, not exact voice clones.
- GitHub Actions validation on Python 3.12: focused voice/security/ops tests **31 passed**; full suite **176 passed, 1 skipped**. Two pre-existing SpeechRecognition deprecation warnings (`aifc`, `audioop`) remain. Run: https://github.com/HareeshGT/KubeDock/actions/runs/39667709768


## Ops Mind voice layout and expressive TTS follow-up (2026-10-22)

- [x] Split engine/persona/voice preview and turn-taking/pacing/volume controls into two rows to prevent horizontal clipping on normal window widths. Preview pauses active hands-free capture, plays the chosen voice, and automatically resumes listening after playback.
- [x] Replaced the empty preview icon-only control with a labeled Preview button; the installed system voice selector is hidden when the neural engine is active.
- [x] Replaced the WebSocket-based speech path with OpenAI's dedicated gpt-4o-mini-tts speech endpoint, which supports explicit vocal delivery instructions and speed. JARVIS-inspired delivery is more measured, resonant and contemporary British-English; FRIDAY is warm and lively; EDITH is composed and precise.
- [x] Removed the unnecessary websocket-client dependency. Neural speech is delivered through Python's standard-library HTTP client in a worker thread with 30-second timeout, bounded WAV response size, validation and credential redaction.
- [x] Changed automatic fallback handling so an API or audio failure does not save System · Offline as the permanent choice. The app uses offline speech for the affected session, preserves Neural · OpenAI as selected, and allows a retry from Preview.
- [x] Added a one-time migration: older unmarked System · Offline values are treated as a previous automatic fallback when an OpenAI key is configured. Explicit offline selections saved by this build remain respected. If Neural · OpenAI is selected before a key is configured, KubeDock preserves that preference so it can be used once the key is added.
- GitHub Actions validation on Python 3.12: focused voice/security/ops tests **31 passed**; full suite **176 passed, 1 skipped**. Two existing SpeechRecognition deprecation warnings (`aifc`, `audioop`) remain. Run: https://github.com/HareeshGT/KubeDock/actions/runs/39671505352


## Neural voice quota error handling (2026-10-22)

- [x] Classify exhausted OpenAI API credits separately from an ordinary HTTP 429 rate limit.
- [x] Show a targeted billing message with the official billing URL when the API reports insufficient quota/no remaining credits.
- [x] Preserve neural preference but fall back to local system speech for the remainder of the session; automatic speech turns do not repeatedly call the paid endpoint. Users can manually retry with Preview after addressing billing.
- [x] Add tests for insufficient-quota classification, billing guidance, redaction of credentials, and distinguishing normal rate limits.
- GitHub Actions validation on Python 3.12: focused voice/security/ops tests **33 passed**; full suite **178 passed, 1 skipped**. Two SpeechRecognition deprecation warnings (`aifc`, `audioop`) remain. Run: https://github.com/HareeshGT/KubeDock/actions/runs/39672886957


## Qwen3-TTS local voice engine (2026-10-22)

### Implementation
- [x] Added a third engine choice, **Qwen3-TTS · Local**, alongside OpenAI neural TTS and System · Offline. Selecting Qwen does not read or require an OpenAI API key.
- [x] Added persona-specific local voice instructions for JARVIS (measured contemporary British-English), FRIDAY (warm conversational delivery with a subtle Irish-English lilt), and EDITH (calm, precise, analytical delivery). These are inspired voice directions, not exact actor/character clones.
- [x] Added model choices: **Voice Design · 1.7B** for natural-language voice design and **Lower memory · 0.6B** for preset speaker voices on lower-memory machines.
- [x] Loads Qwen on demand in a dedicated worker thread and reuses loaded weights between utterances. Handles model-load/inference errors by speaking via the local system voice.
- [x] Uses the current voice/pace settings for Qwen generation, clips output duration, writes temporary WAV files with restrictive permissions, deletes them after playback, and rejects stale audio after interruption/engine change.
- [x] Unloads Qwen weights when the user switches to another TTS engine or exits Ops Mind to free GPU/MPS memory.
- [x] Added optional installation requirements in `requirements-qwen-tts.txt`; the primary `requirements.txt` remains unchanged so the heavier ML stack is not installed for users who do not choose Qwen.
- [x] Added tests for Qwen model IDs, personality instructions, pace handling, local-engine selection without an OpenAI key, settings persistence, and WAV output conversion.

### Install and runtime requirements
- Use an isolated Python 3.12 environment for the optional TTS runtime where possible, then install with `python -m pip install -r requirements-qwen-tts.txt` using the same Python interpreter that launches KubeDock.
- The requirement currently pins an upstream compatibility branch (Qwen3-TTS PR #360) because the PyPI release's exact Transformers dependency conflicts with the current model code. This is an explicitly documented compatibility workaround, not an official upstream release. Check the upstream PR before upgrading that pin: https://github.com/QwenLM/Qwen3-TTS/pull/360
- First use downloads model weights and can take several minutes and multiple GB of disk space. The 1.7B model needs more memory; choose 0.6B if model loading exhausts local memory. CUDA has the most-tested acceleration path; Apple Silicon MPS is attempted when available but should be treated as less-tested than CUDA.
- Speech generation is local after the model weights are downloaded. It does not call OpenAI's paid TTS endpoint. Microphone transcription is unchanged and still sends captured audio to Google Web Speech; this change only localizes speech synthesis.
- If Qwen is not installed, the application remains usable and shows installation guidance. System speech remains available as a fallback. Core dependencies are not changed.
- GitHub Actions on Python 3.12: focused voice/security/ops tests **38 passed, 1 skipped**; full suite **183 passed, 2 skipped**; 2 warnings. Run: https://github.com/HareeshGT/KubeDock/actions/runs/39675318006. The real Qwen model was not downloaded or loaded in CI; model inference still needs verification on the target machine.


## Optional Qwen3-TTS packaging support in installers (2026-10-22)

- [x] Updated `installer/installer.sh` to offer an optional Qwen3-TTS build. `KUBEDOCK_ENABLE_QWEN_TTS=1` enables it for interactive or automated builds; non-interactive installs default to the lightweight build.
- [x] An opted-in macOS build selects Homebrew Python 3.12; Linux and Git Bash/Windows shell builds require Python 3.12. This preserves the existing Python 3.14 default for standard macOS builds.
- [x] Installs and import-verifies `requirements-qwen-tts.txt` only when selected, then uses PyInstaller collection flags for Qwen, PyTorch, Transformers, and their runtime dependencies. Model weights are downloaded on first launch and are not embedded in the bundle.
- [x] Updated `installer/installer-win.py` with the same opt-in choice; Qwen builds select 64-bit Python 3.12 while normal Windows builds retain the existing Python 3.13 requirement.
- [x] Updated `installer/build_pkg.sh` to ask whether to include local Qwen voice during macOS package installation, and fixed propagation of the GUI-selected branch through `KUBEDECK_BRANCH`.
- [x] Core requirements remain unchanged. The optional local model stack adds install time and several GB to the app bundle; it is not installed for users who choose the standard build.
- Validation on GitHub Actions/Python 3.12: `bash -n installer/installer.sh`, `bash -n installer/build_pkg.sh`, and `python -m py_compile installer/installer-win.py` passed; full regression suite **183 passed, 2 skipped** (2 warnings). Run: https://github.com/HareeshGT/KubeDock/actions/runs/39677687192. Qwen model weights/inference are not loaded in CI and still need validation on the target machine.


## Qwen installer Homebrew Python path fix (2026-10-22)

- [x] Fixed macOS Qwen-enabled setup when Homebrew installs Python 3.12 with a versioned `python3.12` executable rather than the unversioned `python3` path.
- [x] Resolve the Homebrew formula prefix and check versioned/unversioned executables in the formula bin, Homebrew bin, and libexec bin directories; verify the selected interpreter is exactly Python 3.12 or 3.14 as configured before proceeding.
- [x] Keep Python 3.14 as the default lightweight macOS build and use Python 3.12 only for opt-in Qwen builds.
- Validation on GitHub Actions/Python 3.12: `bash -n installer/installer.sh`, `bash -n installer/build_pkg.sh`, and `python -m py_compile installer/installer-win.py` passed; versioned Python 3.12 candidate-resolution check passed; full suite **183 passed, 2 skipped, 2 warnings**. Run: https://github.com/HareeshGT/KubeDock/actions/runs/39681782972.


## AI Intelligence Workspace implementation (2026-10-29)

### Implemented on `feat/ai-infrastructure-intelligence`

- [x] Added task-specific provider assignments for Quick explanations, Deep diagnoses, Voice/conversation, and Local/Private inference in Settings → AI. Failed/large command explanations can opt in to escalation to the configured Deep Diagnosis provider.
- [x] Added an Ollama provider pinned to `http://127.0.0.1:11434/v1/chat/completions`. No API key is required; the endpoint is loopback-only and no silent cloud fallback is performed. Pod logs offer an explicit **Analyze Locally** action.
- [x] Added a pod-log **Export Incident** action and an **Incident Investigator** workspace tab. The export creates a timestamped JSON evidence bundle, redacts recognizable credentials, records evidence provenance via a SHA-256 digest, and is not sent to a provider unless the user explicitly loads it into the investigator and starts analysis.
- [x] Added deterministic candidate-signal correlation for OOM, probe failures, image pulls, scheduling, dependency network failures, and permission errors. Signals are presented as hypotheses, not proof, and are attached to the diagnosis context only after redaction.
- [x] Added an AI Intelligence Workspace with a conservative resource right-sizing advisor, JSON snapshot comparison for two clusters, bounded read-only SSH watch mode, remediation proposal validation, and a searchable service-centric index populated either from live services discovered over SSH or from exported service JSON.
- [x] Remediation plans are proposals only. They require user approval, enforce a supported action allowlist and protected namespaces, require explicit delete confirmation, and mark verification as required. No action is executed by the workspace.
- [x] Added opt-in local aggregate AI usage/latency telemetry. It stores provider/profile, calls, errors and average latency only; prompts, logs, responses and API keys are not recorded. It is disabled by default.
- [x] Added focused unit tests for incident evidence redaction, signal correlation, remediation validation, bounded watch configuration, cluster snapshot comparison, conservative right-sizing, service indexing, and opt-in aggregate telemetry.
- [x] Extended the AI model regression workflow to compile the new modules and run AI-focused tests.

### Scope and limitations

- Local inference requires the user to install and start Ollama and pull a model. The app connects only to loopback; no model installation is automated.
- Cluster comparison and service indexing consume user-selected JSON inventory snapshots; automatic snapshot discovery for multiple clusters is not implemented by this workspace.
- The watch mode polls a single named resource with a read-only `kubectl get` command over the existing SSH session. It is finite (maximum one hour), interval-bounded, explicitly started, and performs no automatic remediation.
- Resource right-sizing accepts measured usage and sample count from the user; it does not yet automatically collect historical metrics or write resource requests back to a workload.
- The incident bundle exports the current pod logs plus collected describe/alternate-log/metrics evidence when available. It does not query Kubernetes Secret values.
- The remediation workspace validates proposed actions but does not dispatch them. Existing Ops Mind confirmation, RBAC and post-mutation verification paths remain the only mutation route.
- Voice profile selection applies to AI conversation follow-ups; local/private diagnosis is explicitly selectable from the pod log viewer.
- Full application regression, live cluster behavior, provider connectivity, voice hardware, packaging, and all-platform GUI smoke tests must be confirmed by GitHub Actions and/or on the target machine before merging.
