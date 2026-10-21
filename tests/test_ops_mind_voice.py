import base64
import io
import json
import unittest
import wave
from unittest.mock import patch

from k8s_ai_ops_parts.core import (
    PERSONALITY_CONFIG,
    NEURAL_VOICE_PROFILES,
    OPENAI_REALTIME_TTS_MODEL,
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

    def test_voice_engine_uses_openai_key_when_available_and_safe_fallback(self):
        from k8s_ai_ops_parts import core
        with patch.object(core, "load_settings", return_value={}), patch.object(core.ai_assist, "get_api_key", return_value="sk-test-key"):
            self.assertEqual(get_voice_engine(), "openai")
        with patch.object(core, "load_settings", return_value={}), patch.object(core.ai_assist, "get_api_key", return_value=""):
            self.assertEqual(get_voice_engine(), "system")
        with patch.object(core, "load_settings", return_value={core.VOICE_ENGINE_SETTINGS_KEY: "system"}), patch.object(core.ai_assist, "get_api_key", return_value="sk-test-key"):
            self.assertEqual(get_voice_engine(), "system")

    def test_realtime_worker_wraps_pcm_as_wav_without_network_or_audio_hardware(self):
        from k8s_ai_ops_parts import core
        pcm = (b"\\x00\\x00\\x10\\x00") * 600
        events = [
            {"type": "session.created"},
            {"type": "session.updated"},
            {"type": "response.output_audio.delta", "delta": base64.b64encode(pcm).decode("ascii")},
            {"type": "response.done", "response": {"status": "completed"}},
        ]
        class FakeWebSocket:
            def __init__(self):
                self.sent = []
                self.timeout = None
            def settimeout(self, value):
                self.timeout = value
            def send(self, value):
                self.sent.append(json.loads(value))
            def recv(self):
                if not events:
                    raise AssertionError("Unexpected extra websocket read")
                return json.dumps(events.pop(0))
            def close(self):
                pass
        fake_ws = FakeWebSocket()
        audio, errors = [], []
        worker = _OpenAINeuralTTSWorker("sk-test-key-not-real", "Systems are ready.", "jarvis")
        worker.audio_ready.connect(audio.append)
        worker.error.connect(errors.append)
        with patch.object(core.websocket, "create_connection", return_value=fake_ws) as connect:
            worker.run()
        connect.assert_called_once()
        self.assertIn(OPENAI_REALTIME_TTS_MODEL, connect.call_args.args[0])
        self.assertFalse(errors, errors)
        self.assertEqual(len(audio), 1)
        with wave.open(io.BytesIO(audio[0]), "rb") as wav_file:
            self.assertEqual(wav_file.getframerate(), 24000)
            self.assertEqual(wav_file.getnchannels(), 1)
            self.assertEqual(wav_file.getsampwidth(), 2)
            self.assertEqual(wav_file.readframes(600), pcm)
        session = fake_ws.sent[0]["session"]
        self.assertEqual(session["audio"]["output"]["voice"], "cedar")
        self.assertEqual(session["output_modalities"], ["audio"])

    def test_personalities_have_distinct_style_and_voice_profiles(self):
        self.assertEqual(set(PERSONALITY_CONFIG), {"jarvis", "friday", "edith"})
        self.assertEqual(len({config["rate"] for config in PERSONALITY_CONFIG.values()}), 3)
        styles = {personality_prompt_style(key) for key in PERSONALITY_CONFIG}
        self.assertEqual(len(styles), 3)
        self.assertIn("security-conscious", personality_prompt_style("edith"))


if __name__ == "__main__":
    unittest.main()
