"""Helpers for interpreting command feedback shown in the Kubernetes UI."""


_ERROR_PREFIXES = ("error:", "error from server")
_ERROR_PHRASES = ("unable to connect to the server", "the connection to the server")


def command_output_has_error(output):
  """Return True for common kubectl error output without flagging empty results."""
  for line in str(output or "").splitlines():
    normalized = line.strip().lower()
    if normalized.startswith(_ERROR_PREFIXES):
      return True
    if any(phrase in normalized for phrase in _ERROR_PHRASES):
      return True
  return False
