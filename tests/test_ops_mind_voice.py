import io
import json
import unittest
import wave
from unittest.mock import patch

from k8s_ai_ops_parts.core import (
    PERSONALITY_CONFIG,
    NEURAL_VOICE_PROFILES,
    QWEN_VOICEDESIGN_MODEL_ID,
    QWEN_CUSTOMVOICE_MODEL_ID,
    QWEN_TTS_MODEL_SETTINGS_KEY,
    _Qwen3TTSWorker,
    get_qwen_tts_model_config,
    get_qwen_voice_profile,
    save_qwen_tts_model,
    OPENAI_NEURAL_TTS_MODEL,
    _OpenAINeuralTTSWorker,
    get_neural_voice_profile,
    get_voice_engine,
    VoiceConversationStateMachine,
    is_duplicate_voice_transcript,
    normalize_voice_transcript,
    personality_prompt_style,
    personalize_local_response,
    personalize_spoken_phrase,
    should_stop_for_silence,
    should_timeout_without_speech,
    speech_friendly_text,
    validate_action,
    build_kubectl_command,
    _build_k8s_ops_prompt,
)


class VoiceConversationTests(unittest.TestCase):
    def test_voice_state_machine_tracks_normal_conversation_cycle(self):
        machine = VoiceConversationStateMachine()
        for state in ("LISTENING", "TRANSCRIBING", "THINKING", "SPEAKING", "INTERRUPTED", "LISTENING", "CLOSED"):
            self.assertTrue(machine.transition(state), state)
        self.assertEqual(machine.state, "CLOSED")
        self.assertFalse(machine.transition("IDLE"))

    def test_voice_state_machine_rejects_invalid_transition(self):
        machine = VoiceConversationStateMachine()
        self.assertFalse(machine.transition("TRANSCRIBING"))
        self.assertEqual(machine.state, "IDLE")
        with self.assertRaises(ValueError):
            machine.transition("MAGIC")

    def test_duplicate_transcripts_normalize_case_and_punctuation(self):
        self.assertEqual(normalize_voice_transcript("Restart Deployment/API-1!"), "restart deployment/api-1")
        self.assertTrue(
            is_duplicate_voice_transcript(
                "Restart deployment/api-1!",
                "restart deployment/API-1",
                now=103.0,
                previous_at=100.0,
            )
        )

    def test_repeated_words_after_duplicate_window_are_allowed(self):
        self.assertFalse(
            is_duplicate_voice_transcript(
                "restart deployment/api-1",
                "restart deployment/api-1",
                now=106.0,
                previous_at=100.0,
            )
        )

    def test_vad_stops_only_after_detected_speech_and_silence(self):
        self.assertFalse(should_stop_for_silence(None, None, 20.0, 1.0))
        self.assertFalse(should_stop_for_silence(1.0, 5.0, 5.6, 0.7))
        self.assertTrue(should_stop_for_silence(1.0, 5.0, 5.8, 0.7))

    def test_no_speech_timeout_is_bounded(self):
        self.assertFalse(should_timeout_without_speech(None, 5.0, 16.9, 12.0))
        self.assertTrue(should_timeout_without_speech(None, 5.0, 17.0, 12.0))
        self.assertFalse(should_timeout_without_speech(6.0, 5.0, 30.0, 12.0))

    def test_tts_sanitizer_does_not_speak_markdown_code(self):
        text = speech_friendly_text("Try this:\n```bash\nkubectl delete pod api-1\n```\nThen verify readiness.")
        self.assertNotIn("kubectl delete pod", text)
        self.assertNotIn("```", text)
        self.assertIn("written response", text)
        self.assertIn("verify readiness", text)

    def test_tts_sanitizer_replaces_raw_json(self):
        text = speech_friendly_text('{"action":"delete","resource":"pod","name":"api-1"}')
        self.assertEqual(text, "The structured operation details are available in the written response.")

    def test_local_responses_change_with_selected_personality(self):
        base = "Hello! Tell me what you want to do in Kubernetes."
        self.assertIn("investigate", personalize_local_response(base, "jarvis"))
        self.assertIn("check", personalize_local_response(base, "friday"))
        self.assertIn("online", personalize_local_response(base, "edith"))

    def test_spoken_operation_results_are_conversational(self):
        self.assertEqual(personalize_spoken_phrase("Get completed.", "jarvis"), "The requested information is ready.")
        self.assertIn("found", personalize_spoken_phrase("Get completed.", "friday"))
        self.assertIn("complete", personalize_spoken_phrase("Get completed.", "edith"))
        self.assertIn("unconfirmed", personalize_spoken_phrase("Restart failed.", "jarvis"))

    def test_explicit_system_default_voice_does_not_fall_back_to_legacy_voice(self):
        from k8s_ai_ops_parts import core
        with patch.object(core, "load_settings", return_value={
            core.PERSONALITY_VOICES_SETTINGS_KEY: {"friday": ""},
            core.VOICE_ID_SETTINGS_KEY: "legacy-non-default-voice",
        }):
            self.assertEqual(core.get_voice_id("friday"), "")
    def test_conversation_response_is_typed_and_bounded(self):
        action = validate_action({"action": "conversation", "response": "The earlier output indicates a scheduling error."})
        self.assertEqual(action["kind"], "conversation")
        self.assertIn("scheduling error", action["response"])
        clipped = validate_action({"action": "conversation", "response": "x" * 2000})
        self.assertLessEqual(len(clipped["response"]), 1201)

    def test_prompt_includes_bounded_recent_context_without_changing_ops_schema(self):
        prompt = _build_k8s_ops_prompt(
            "Why did that happen?", "prod", "cluster context", [],
            conversation=[
                {"role": "user", "content": "describe pod payments-1"},
                {"role": "assistant", "content": "Events show BackOff and the pod is not ready."},
            ],
        )
        self.assertIn("recent_conversation", prompt)
        self.assertIn("Events show BackOff", prompt)
        self.assertIn('"action":"conversation"', prompt)
        self.assertIn("use exactly one allow-listed operation", prompt)

    def test_logs_action_builds_bounded_command_and_validates_container(self):
        action = validate_action({
            "action": "logs", "resource": "pod", "name": "payments-api-1",
            "namespace": "prod", "previous": True, "container": "api",
        })
        command = build_kubectl_command(action)
        self.assertIn("logs --tail=100 --previous -c api payments-api-1", command)
        self.assertIn("-n prod", command)
        with self.assertRaises(ValueError):
            validate_action({"action": "logs", "resource": "deployment", "name": "payments", "namespace": "prod"})
        with self.assertRaises(ValueError):
            validate_action({"action": "logs", "resource": "pod", "name": "payments-1", "namespace": "prod", "container": "api; rm -rf /"})

    def test_logs_require_a_namespace(self):
        with self.assertRaises(ValueError):
            validate_action({"action": "logs", "resource": "pod", "name": "payments-1", "namespace": ""})

    def test_neural_voice_profiles_have_distinct_voices_and_instructions(self):
        self.assertEqual(set(NEURAL_VOICE_PROFILES), {"jarvis", "friday", "edith"})
        self.assertEqual(len({p["voice"] for p in NEURAL_VOICE_PROFILES.values()}), 3)
        self.assertEqual(len({p["instructions"] for p in NEURAL_VOICE_PROFILES.values()}), 3)
        self.assertIn("British-English", get_neural_voice_profile("jarvis")["instructions"])
        self.assertIn("Irish lilt", get_neural_voice_profile("friday")["instructions"])
        self.assertIn("analytical", get_neural_voice_profile("edith")["instructions"])
        self.assertLess(get_neural_voice_profile("jarvis", "slow")["speed"], get_neural_voice_profile("jarvis", "natural")["speed"])
        self.assertGreater(get_neural_voice_profile("friday", "brisk")["speed"], get_neural_voice_profile("friday", "natural")["speed"])
        self.assertIn("unhurried pace", get_neural_voice_profile("jarvis", "slow")["instructions"])
        self.assertIn("brisk, energetic pace", get_neural_voice_profile("jarvis", "brisk")["instructions"])

    def test_tts_quota_error_has_specific_billing_guidance(self):
        body = json.dumps({
            "error": {
                "message": "You have no credits remaining. Add credits to continue.",
                "type": "billing_quota",
                "code": "insufficient_quota",
            }
        }).encode("utf-8")
        message = _OpenAINeuralTTSWorker._safe_api_error(body, "sk-test-secret", 429)
        self.assertIn("API credits are exhausted", message)
        self.assertIn("https://platform.openai.com/settings/organization/billing/", message)
        self.assertIn("click Preview", message)
        self.assertNotIn("sk-test-secret", message)

    def test_tts_rate_limit_is_not_misreported_as_exhausted_credits(self):
        body = json.dumps({
            "error": {
                "message": "Rate limit reached for requests.",
                "type": "requests",
                "code": "rate_limit_exceeded",
            }
        }).encode("utf-8")
        message = _OpenAINeuralTTSWorker._safe_api_error(body, "sk-test-secret", 429)
        self.assertIn("rate limit reached", message.lower())
        self.assertNotIn("credits are exhausted", message.lower())
    def test_neural_tts_redacts_credentials_before_external_request(self):
        worker = _OpenAINeuralTTSWorker(
            "sk-test-private-key",
            "The password=supersecret99 was supplied. Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
            "jarvis",
        )
        self.assertNotIn("supersecret99", worker._text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", worker._text)
        self.assertNotIn("sk-test-private-key", worker._text)
        self.assertIn("[REDACTED]", worker._text)

    def test_voice_engine_uses_openai_key_when_available_and_safe_fallback(self):
        from k8s_ai_ops_parts import core
        with patch.object(core, "load_settings", return_value={}), patch.object(core.ai_assist, "get_api_key", return_value="sk-test-key"):
            self.assertEqual(get_voice_engine(), "openai")
        with patch.object(core, "load_settings", return_value={}), patch.object(core.ai_assist, "get_api_key", return_value=""):
            self.assertEqual(get_voice_engine(), "system")
        # An unmarked "system" value can be an automatic fallback from an older build.
        with patch.object(core, "load_settings", return_value={core.VOICE_ENGINE_SETTINGS_KEY: "system"}), patch.object(core, "save_settings") as save_settings, patch.object(core.ai_assist, "get_api_key", return_value="sk-test-key"):
            self.assertEqual(get_voice_engine(), "openai")
            save_settings.assert_called_once()
        # A current, explicit offline choice must still be respected.
        with patch.object(core, "load_settings", return_value={
            core.VOICE_ENGINE_SETTINGS_KEY: "system",
            core.VOICE_ENGINE_EXPLICIT_SETTINGS_KEY: True,
        }), patch.object(core.ai_assist, "get_api_key", return_value="sk-test-key"):
            self.assertEqual(get_voice_engine(), "system")

    def test_neural_tts_uses_expressive_speech_endpoint_and_wav_output(self):
        from k8s_ai_ops_parts import core
        pcm = bytes((0, 0, 16, 0)) * 300
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(24000)
            wav_file.writeframes(pcm)
        wav_bytes = buffer.getvalue()

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload
                self.headers = {"Content-Length": str(len(payload))}
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self, size=-1):
                if size is None or size < 0:
                    size = len(self._payload)
                data, self._payload = self._payload[:size], self._payload[size:]
                return data

        audio, errors = [], []
        worker = _OpenAINeuralTTSWorker("sk-test-key-not-real", "Systems are ready.", "jarvis")
        worker.audio_ready.connect(audio.append)
        worker.error.connect(errors.append)
        with patch.object(core.urllib.request, "urlopen", return_value=FakeResponse(wav_bytes)) as request:
            worker.run()
        request.assert_called_once()
        req = request.call_args.args[0]
        self.assertEqual(req.full_url, "https://api.openai.com/v1/audio/speech")
        payload = json.loads(req.data.decode("utf-8"))
        self.assertEqual(payload["model"], OPENAI_NEURAL_TTS_MODEL)
        self.assertEqual(payload["voice"], "cedar")
        self.assertEqual(payload["response_format"], "wav")
        self.assertLess(payload["speed"], 1.0)
        self.assertIn("contemporary British-English", payload["instructions"])
        self.assertFalse(errors, errors)
        self.assertEqual(audio, [wav_bytes])
    def test_qwen3_tts_profiles_are_local_and_personality_specific(self):
        jarvis = get_qwen_voice_profile("jarvis", "natural")
        friday = get_qwen_voice_profile("friday", "natural")
        edith = get_qwen_voice_profile("edith", "natural")
        self.assertIn("British-English", jarvis["instructions"])
        self.assertIn("Irish-English", friday["instructions"])
        self.assertIn("analytical", edith["instructions"].lower())
        self.assertEqual({"jarvis": jarvis["speaker"], "friday": friday["speaker"], "edith": edith["speaker"]}, {"jarvis": "Ryan", "friday": "Vivian", "edith": "Aiden"})
        self.assertNotEqual(jarvis["instructions"], friday["instructions"])
        self.assertNotEqual(friday["instructions"], edith["instructions"])
        self.assertIn("deliberately unhurried", get_qwen_voice_profile("jarvis", "slow")["instructions"])
        self.assertIn("brisk, energetic", get_qwen_voice_profile("edith", "brisk")["instructions"])

    def test_qwen_model_config_uses_voice_design_and_lower_memory_custom_voice(self):
        design = get_qwen_tts_model_config("voice_design")
        compact = get_qwen_tts_model_config("custom_voice")
        self.assertEqual(design["model_id"], QWEN_VOICEDESIGN_MODEL_ID)
        self.assertTrue(design["voice_design"])
        self.assertTrue(design["supports_voice_instructions"])
        self.assertEqual(compact["model_id"], QWEN_CUSTOMVOICE_MODEL_ID)
        self.assertFalse(compact["voice_design"])
        self.assertFalse(compact["supports_voice_instructions"])
        self.assertEqual(compact["key"], "custom_voice")

    def test_qwen_engine_is_independent_of_openai_credits_or_key(self):
        from k8s_ai_ops_parts import core
        with patch.object(core, "load_settings", return_value={core.VOICE_ENGINE_SETTINGS_KEY: "qwen"}), patch.object(core.ai_assist, "get_api_key", return_value=""):
            self.assertEqual(get_voice_engine(), "qwen")

    def test_qwen_model_preference_is_saved_through_existing_settings(self):
        from k8s_ai_ops_parts import core
        with patch.object(core, "save_settings") as save:
            save_qwen_tts_model("custom_voice")
        save.assert_called_once_with(**{QWEN_TTS_MODEL_SETTINGS_KEY: "custom_voice"})

    def test_qwen_worker_assigns_ids_to_ignore_stale_audio_after_interruptions(self):
        worker = _Qwen3TTSWorker()
        first = worker.enqueue("hello", "jarvis", "natural", "voice_design")
        second = worker.enqueue("goodbye", "friday", "brisk", "custom_voice")
        self.assertGreater(second, first)
        first_job = worker._requests.get_nowait()
        second_job = worker._requests.get_nowait()
        self.assertEqual(first_job[0], first)
        self.assertEqual(second_job[0], second)
        self.assertEqual(first_job[1], "hello")
        self.assertEqual(second_job[1], "goodbye")
    def test_qwen_wav_conversion_creates_a_valid_pcm_wav(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("NumPy is installed with the optional Qwen3-TTS runtime.")
        wav_bytes = _Qwen3TTSWorker._to_wav_bytes(np.array([0.0, 0.25, -0.25, 0.5], dtype=np.float32), 24000)
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            self.assertEqual(wf.getnchannels(), 1)
            self.assertEqual(wf.getsampwidth(), 2)
            self.assertEqual(wf.getframerate(), 24000)
            self.assertEqual(wf.getnframes(), 4)

    def test_personalities_have_distinct_style_and_voice_profiles(self):
        self.assertEqual(set(PERSONALITY_CONFIG), {"jarvis", "friday", "edith"})
        self.assertEqual(len({config["rate"] for config in PERSONALITY_CONFIG.values()}), 3)
        styles = {personality_prompt_style(key) for key in PERSONALITY_CONFIG}
        self.assertEqual(len(styles), 3)
        self.assertIn("security-conscious", personality_prompt_style("edith"))


if __name__ == "__main__":
    unittest.main()
