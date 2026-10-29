from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

PROFILE_QUICK = 'quick'
PROFILE_DEEP_DIAGNOSIS = 'deep_diagnosis'
PROFILE_VOICE = 'voice'
PROFILE_LOCAL_PRIVATE = 'local_private'
VALID_PROFILES = frozenset({PROFILE_QUICK, PROFILE_DEEP_DIAGNOSIS, PROFILE_VOICE, PROFILE_LOCAL_PRIVATE})

@dataclass(frozen=True)
class ModelSelection:
    provider: str
    model_id: str
    profile: str
    reason: str


def resolve_model_profile(profile: str, assignments: Mapping[str, Mapping[str, str]], *, default_provider: Optional[str] = None, default_model: Optional[str] = None, auto_escalation: bool = False, task_requires_deep_reasoning: bool = False) -> ModelSelection:
    """Resolve configured task routing without silently substituting a model.

    assignments maps profile names to provider/model_id pairs already validated
    against application settings. Missing assignments raise ValueError.
    """
    requested = str(profile or '').strip().lower()
    if requested not in VALID_PROFILES:
        raise ValueError(f'Unknown AI task profile: {requested or "(empty)"}')
    effective = requested
    reason = f'User-selected {requested} profile'
    if requested == PROFILE_QUICK and auto_escalation and task_requires_deep_reasoning:
        effective = PROFILE_DEEP_DIAGNOSIS
        reason = 'Escalated from Quick because the caller marked this task complex'
    assignment = assignments.get(effective)
    if not isinstance(assignment, Mapping):
        raise ValueError(f'No model is configured for the {effective} profile')
    provider = str(assignment.get('provider') or '').strip().lower()
    model_id = str(assignment.get('model_id') or '').strip()
    if not provider or not model_id:
        raise ValueError(f'The {effective} profile has an incomplete model assignment')
    if effective == PROFILE_LOCAL_PRIVATE and provider not in {'local', 'ollama', 'openai_compatible'}:
        raise ValueError('Local / Private must use a configured local or compatible endpoint')
    if default_provider and default_model:
        return ModelSelection(str(default_provider).strip().lower(), str(default_model).strip(), effective, 'Single-model override is enabled')
    return ModelSelection(provider, model_id, effective, reason)
