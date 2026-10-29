import unittest
from ai_task_profiles import PROFILE_DEEP_DIAGNOSIS, PROFILE_LOCAL_PRIVATE, PROFILE_QUICK, PROFILE_VOICE, resolve_model_profile

ASSIGNMENTS = {
    PROFILE_QUICK: {'provider': 'groq', 'model_id': 'quick-model'},
    PROFILE_DEEP_DIAGNOSIS: {'provider': 'anthropic', 'model_id': 'deep-model'},
    PROFILE_VOICE: {'provider': 'mistral', 'model_id': 'voice-model'},
    PROFILE_LOCAL_PRIVATE: {'provider': 'ollama', 'model_id': 'local-model'},
}

class ResolveModelProfileTests(unittest.TestCase):
    def test_explicit_quick_assignment(self):
        result = resolve_model_profile(PROFILE_QUICK, ASSIGNMENTS)
        self.assertEqual((result.provider, result.model_id), ('groq', 'quick-model'))

    def test_escalation_is_opt_in(self):
        result = resolve_model_profile(PROFILE_QUICK, ASSIGNMENTS, task_requires_deep_reasoning=True)
        self.assertEqual(result.profile, PROFILE_QUICK)

    def test_enabled_escalation_uses_deep_assignment(self):
        result = resolve_model_profile(PROFILE_QUICK, ASSIGNMENTS, auto_escalation=True, task_requires_deep_reasoning=True)
        self.assertEqual((result.profile, result.model_id), (PROFILE_DEEP_DIAGNOSIS, 'deep-model'))

    def test_voice_assignment(self):
        self.assertEqual(resolve_model_profile(PROFILE_VOICE, ASSIGNMENTS).model_id, 'voice-model')

    def test_missing_profile_fails_without_fallback(self):
        with self.assertRaisesRegex(ValueError, 'No model is configured'):
            resolve_model_profile(PROFILE_QUICK, {})

    def test_incomplete_assignment_fails(self):
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            resolve_model_profile(PROFILE_QUICK, {PROFILE_QUICK: {'provider': 'groq', 'model_id': ''}})

    def test_local_profile_rejects_cloud_provider(self):
        with self.assertRaisesRegex(ValueError, 'Local / Private'):
            resolve_model_profile(PROFILE_LOCAL_PRIVATE, {PROFILE_LOCAL_PRIVATE: {'provider': 'openai', 'model_id': 'cloud-model'}})

    def test_single_model_override_is_explicit(self):
        result = resolve_model_profile(PROFILE_QUICK, ASSIGNMENTS, default_provider='mistral', default_model='pinned-model')
        self.assertEqual((result.provider, result.model_id), ('mistral', 'pinned-model'))
        self.assertIn('override', result.reason)

if __name__ == '__main__':
    unittest.main()
