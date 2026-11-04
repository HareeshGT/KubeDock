"""Small, testable helpers for Kubernetes resource navigation context."""


def build_navigation_path(context, namespace, tab, resource=None):
  """Format the active context → namespace → tab → optional resource path."""
  context = (context or "").strip() or "Not selected"
  namespace = (namespace or "").strip() or "Not selected"
  tab = (tab or "").strip() or "Resources"
  parts = [f"Context: {context}", f"Namespace: {namespace}", tab]
  resource = (resource or "").strip()
  if resource:
    parts.append(resource)
  return "  ›  ".join(parts)
