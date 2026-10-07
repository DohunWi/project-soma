"""Session-scoped coordinator for the pure logical Feedback Policy."""
import json
import threading
from pathlib import Path
from uuid import UUID

from jsonschema import Draft202012Validator, FormatChecker

from feedback.config import FeedbackPolicyConfig
from feedback.policy.break_policy import FeedbackPolicyState, step as feedback_step


ROOT = Path(__file__).resolve().parents[1]
FEEDBACK_DECISION_SCHEMA_PATH = (
    ROOT / "docs" / "contracts" / "feedback_decision.schema.json"
)


def _load_validator():
    schema = json.loads(FEEDBACK_DECISION_SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


FEEDBACK_DECISION_VALIDATOR = _load_validator()


class FeedbackDecisionError(ValueError):
    """The policy output does not satisfy the logical feedback contract."""


class FeedbackSessionNotActive(LookupError):
    """No Feedback Policy state exists for the requested measurement session."""


class FeedbackCoordinator:
    """Own one immutable FeedbackPolicyState per active measurement session."""

    def __init__(
        self,
        config: FeedbackPolicyConfig,
        *,
        validator=FEEDBACK_DECISION_VALIDATOR,
        policy_step=None,
    ):
        self._config = config
        self._validator = validator
        self._policy_step = policy_step or feedback_step
        self._states = {}
        self._lock = threading.RLock()

    @property
    def config(self):
        return self._config

    def start_session(self, session_id):
        """Create fresh policy state for a newly created measurement session."""
        session_id = _session_uuid(session_id)
        with self._lock:
            self._states[session_id] = FeedbackPolicyState()

    def stop_session(self, session_id):
        """Discard all policy accumulation at the measurement boundary."""
        session_id = _session_uuid(session_id)
        with self._lock:
            self._states.pop(session_id, None)

    def state_for(self, session_id):
        """Return immutable state for diagnostics and lifecycle tests."""
        session_id = _session_uuid(session_id)
        with self._lock:
            return self._states.get(session_id)

    def process_decision(self, session_id, fusion_decision, now):
        """Advance, validate, and commit one session's logical feedback decision."""
        session_id = _session_uuid(session_id)
        with self._lock:
            previous = self._states.get(session_id)
            if previous is None:
                raise FeedbackSessionNotActive(
                    f"feedback session is not active: {session_id}"
                )
            current, decision = self._policy_step(
                previous,
                fusion_decision,
                now,
                session_id,
                config=self._config,
            )
            message = _validation_message(self._validator, decision)
            if message:
                raise FeedbackDecisionError(
                    f"feedback_decision contract violation: {message}"
                )
            # Invalid output must not advance the session checkpoint.
            self._states[session_id] = current
            return decision


def _session_uuid(session_id):
    return UUID(str(session_id))


def _validation_message(validator, payload):
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if not errors:
        return None
    error = errors[0]
    location = ".".join(str(part) for part in error.absolute_path) or "<root>"
    return f"{location}: {error.message}"
