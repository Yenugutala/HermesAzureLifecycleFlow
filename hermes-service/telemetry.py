"""
FinOps + Observability telemetry for Hermes Service.

Tracks per-request metrics: user identity, skill executed, LLM token cost,
Jira API operations, and duration. Emits to:
  1. Console (always) — for local dev / container logs
  2. Azure Application Insights (when APPLICATIONINSIGHTS_CONNECTION_STRING is set)

Usage:
    tracker = RequestTracker(user_email="user@example.com", user_role="ba")
    tracker.set_skill("list_tickets")
    tracker.add_tokens("anthropic/claude-haiku-4-5", input_tokens=500, output_tokens=120)
    tracker.add_jira_ops(api_calls=1)
    tracker.finish(success=True)
    tracker.emit()
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

# ── Token cost rates (USD per 1 million tokens) ───────────────────────────────
# Source: OpenRouter pricing page (approximate, update as needed)
MODEL_RATES: dict[str, tuple[float, float]] = {
    # model_id: (input_$/1M, output_$/1M)
    "anthropic/claude-haiku-4-5":        (0.25,  1.25),
    "anthropic/claude-haiku-4-5-20251001": (0.25, 1.25),
    "anthropic/claude-sonnet-4-6":       (3.00, 15.00),
    "anthropic/claude-opus-4-6":         (15.00, 75.00),
    "openai/gpt-4o-mini":                (0.15,  0.60),
    "openai/gpt-4o":                     (5.00, 15.00),
    "google/gemini-flash-1.5":           (0.075, 0.30),
}

EVENT_TYPE = "HERMES_FINOPS"


# ── Data model ────────────────────────────────────────────────────────────────

@dataclasses.dataclass
class RequestEvent:
    user_email: str = ""
    user_role: str = "unknown"
    skill_name: str = "unknown"
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    llm_cost_usd: float = 0.0
    jira_api_calls: int = 0
    jira_tickets_created: int = 0
    jira_epics_created: int = 0
    duration_ms: int = 0
    success: bool = True
    error_msg: str = ""
    correlation_id: str = ""

    def to_dict(self) -> dict:
        return {
            "event_type": EVENT_TYPE,
            **dataclasses.asdict(self),
        }


# ── Tracker ───────────────────────────────────────────────────────────────────

class RequestTracker:
    """
    Context tracker for a single Slack → Hermes → Jira request cycle.
    Create one instance per message, call methods to accumulate stats,
    then call finish() + emit() at the end.
    """

    def __init__(self, user_email: str = "", user_role: str = "unknown",
                 correlation_id: str = "") -> None:
        self.event = RequestEvent(user_email=user_email, user_role=user_role,
                                  correlation_id=correlation_id)
        self._start_ts = time.time()

    def set_skill(self, skill_name: str) -> None:
        """Record which intent/skill the agent executed."""
        self.event.skill_name = skill_name or "unknown"

    def add_tokens(self, model: str, input_tokens: int, output_tokens: int) -> None:
        """
        Accumulate LLM token counts and calculate cost in USD.
        Can be called multiple times (e.g. intent call + BRD call) — totals accumulate.
        """
        self.event.model = model
        self.event.input_tokens += input_tokens
        self.event.output_tokens += output_tokens

        rates = MODEL_RATES.get(model, (0.0, 0.0))
        cost = (input_tokens / 1_000_000) * rates[0] + (output_tokens / 1_000_000) * rates[1]
        self.event.llm_cost_usd += cost

    def add_jira_ops(
        self,
        api_calls: int = 1,
        tickets: int = 0,
        epics: int = 0,
    ) -> None:
        """Accumulate Jira operation counts."""
        self.event.jira_api_calls += api_calls
        self.event.jira_tickets_created += tickets
        self.event.jira_epics_created += epics

    def finish(self, success: bool = True, error: str = "") -> None:
        """Stop the timer and record success/failure."""
        elapsed_ms = int((time.time() - self._start_ts) * 1000)
        self.event.duration_ms = elapsed_ms
        self.event.success = success
        self.event.error_msg = error

    def emit(self) -> None:
        """
        Emit the telemetry event to:
          1. Console/log (always)
          2. Azure Application Insights (if connection string is configured)
        """
        ev = self.event
        console_line = (
            f"[{ev.correlation_id or 'no-corr-id'}] [TELEMETRY] "
            f"user={ev.user_email or 'anonymous'} "
            f"role={ev.user_role} skill={ev.skill_name} "
            f"model={ev.model or 'n/a'} "
            f"tokens=in:{ev.input_tokens}/out:{ev.output_tokens} "
            f"cost=${ev.llm_cost_usd:.6f} "
            f"jira_calls={ev.jira_api_calls} "
            f"epics={ev.jira_epics_created} stories={ev.jira_tickets_created} "
            f"duration={ev.duration_ms}ms "
            f"success={ev.success}"
        )
        if ev.error_msg:
            console_line += f" error={ev.error_msg!r}"
        log.info(console_line)

        _emit_to_app_insights(ev)


# ── Azure Application Insights sink ──────────────────────────────────────────

_ai_handler: Optional[logging.Handler] = None
_ai_logger: Optional[logging.Logger] = None


def _get_ai_logger() -> Optional[logging.Logger]:
    """Lazily build the App Insights logger (one-time setup)."""
    global _ai_handler, _ai_logger

    if _ai_logger is not None:
        return _ai_logger

    conn_str = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    if not conn_str:
        log.debug("APPLICATIONINSIGHTS_CONNECTION_STRING not set — App Insights disabled")
        return None

    try:
        from opencensus.ext.azure.log_exporter import AzureLogHandler  # type: ignore

        _ai_logger = logging.getLogger("hermes_telemetry")
        _ai_logger.setLevel(logging.INFO)
        # Avoid duplicate handlers on repeated calls
        if not _ai_logger.handlers:
            handler = AzureLogHandler(connection_string=conn_str)
            _ai_logger.addHandler(handler)
            _ai_handler = handler
        log.info("Azure Application Insights telemetry enabled")
        return _ai_logger
    except ImportError:
        log.warning(
            "opencensus-ext-azure not installed — "
            "run: pip install opencensus-ext-azure==1.1.13"
        )
        return None
    except Exception as exc:
        log.warning("Failed to initialise App Insights handler: %s", exc)
        return None


def _emit_to_app_insights(event: RequestEvent) -> None:
    """Send the event to App Insights as a structured trace with customDimensions."""
    ai_logger = _get_ai_logger()
    if ai_logger is None:
        return

    try:
        # opencensus AzureLogHandler picks up 'custom_dimensions' from extra
        dimensions = {k: str(v) for k, v in event.to_dict().items()}
        ai_logger.info(
            EVENT_TYPE,
            extra={"custom_dimensions": dimensions},
        )
    except Exception as exc:
        log.warning("App Insights emit failed: %s", exc)
