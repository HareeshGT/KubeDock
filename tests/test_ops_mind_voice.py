import unittest

from k8s_ai_ops_parts.core import (
    PERSONALITY_CONFIG,
    VoiceConversationStateMachine,
    is_duplicate_voice_transcript,
    normalize_voice_transcript,
    personality_prompt_style,
    should_stop_for_silence,
    should_timeout_without_speech,
    speech_friendly_text,
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

    def test_personalities_have_distinct_style_and_voice_profiles(self):
        self.assertEqual(set(PERSONALITY_CONFIG), {"jarvis", "friday", "edith"})
        self.assertEqual(len({config["rate"] for config in PERSONALITY_CONFIG.values()}), 3)
        styles = {personality_prompt_style(key) for key in PERSONALITY_CONFIG}
        self.assertEqual(len(styles), 3)
        self.assertIn("security-conscious", personality_prompt_style("edith"))


if __name__ == "__main__":
    unittest.main()
