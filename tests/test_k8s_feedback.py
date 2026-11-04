import unittest

from k8s_feedback import command_output_has_error


class KubernetesFeedbackTests(unittest.TestCase):
  def test_detects_kubectl_error_prefixes(self):
    self.assertTrue(command_output_has_error("error: the server doesn't have a resource type"))
    self.assertTrue(command_output_has_error("Error from server (Forbidden): access denied"))

  def test_detects_connection_failures(self):
    self.assertTrue(command_output_has_error("Unable to connect to the server: timeout"))

  def test_empty_results_are_not_errors(self):
    self.assertFalse(command_output_has_error("No resources found in default namespace."))
    self.assertFalse(command_output_has_error(""))
    self.assertFalse(command_output_has_error("NAME READY STATUS\napi 1/1 Running"))

  def test_ignores_error_word_in_normal_resource_data(self):
    self.assertFalse(command_output_has_error("NAME MESSAGE\napi error-budget-config"))


if __name__ == "__main__":
  unittest.main()
