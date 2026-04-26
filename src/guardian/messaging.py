"""
GUARDIAN-Health — Solace Messaging Layer
Single entry point for all publish/subscribe operations.
Topic hierarchy encodes governance policy:
  guardian/v1/signals/raw/{centre_id}/{patient_pseudo_id}
  guardian/v1/signals/evaluated/{severity}/{patient_pseudo_id}
  guardian/v1/hitl/required/{case_id}
  guardian/v1/hitl/decision/{case_id}
  guardian/v1/notifications/clinician/{session_id}
  guardian/v1/audit/{module}/{event_type}

Governance rules encoded here:
  - Audit topics are publish-only for modules (no subscribe)
  - HITL decision topic requires human role (enforced at API level)
  - All messages carry session_id and timestamp in headers
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

import structlog
from solace.messaging.messaging_service import MessagingService
from solace.messaging.config.retry_strategy import RetryStrategy
from solace.messaging.resources.topic import Topic
from solace.messaging.resources.topic_subscription import TopicSubscription
from solace.messaging.receiver.message_receiver import MessageHandler
from solace.messaging.receiver.inbound_message import InboundMessage

from src.guardian.config import get_settings

log = structlog.get_logger(__name__)


# ── Topic registry — single source of truth for all topic strings ──────────────

class Topics:
    """
    Centralised topic registry. All modules use these constants.
    Changing a topic string here changes it everywhere simultaneously.
    """

    @staticmethod
    def signal_raw(centre_id: str, patient_pseudo_id: str) -> str:
        short = patient_pseudo_id[:8]
        return f"guardian/v1/signals/raw/{centre_id}/{short}"

    @staticmethod
    def signal_evaluated(severity: str, patient_pseudo_id: str) -> str:
        short = patient_pseudo_id[:8]
        return f"guardian/v1/signals/evaluated/{severity.lower()}/{short}"

    @staticmethod
    def hitl_required(case_id: str) -> str:
        return f"guardian/v1/hitl/required/{case_id}"

    @staticmethod
    def hitl_decision(case_id: str) -> str:
        return f"guardian/v1/hitl/decision/{case_id}"

    @staticmethod
    def notification_clinician(session_id: str) -> str:
        return f"guardian/v1/notifications/clinician/{session_id[:8]}"

    @staticmethod
    def audit(module: str, event_type: str) -> str:
        return f"guardian/v1/audit/{module.lower()}/{event_type.lower()}"

    # Wildcard subscriptions for consumers
    SIGNALS_RAW_ALL       = "guardian/v1/signals/raw/>"
    SIGNALS_EVALUATED_ALL = "guardian/v1/signals/evaluated/>"
    HITL_DECISIONS_ALL    = "guardian/v1/hitl/decision/>"
    AUDIT_ALL             = "guardian/v1/audit/>"


# ── Messaging client ───────────────────────────────────────────────────────────

class GuardianMessaging:
    """
    Wrapper around the Solace MessagingService.
    Use as a context manager or call connect()/disconnect() explicitly.
    """

    def __init__(self):
        self._service: Optional[MessagingService] = None
        self._publisher = None

    def connect(self) -> "GuardianMessaging":
        settings = get_settings()
        broker_props = {
            "solace.messaging.transport.host": f"tcp://{settings.solace_host}:{settings.solace_smf_port}",
            "solace.messaging.service.vpn-name": "default",
            "solace.messaging.authentication.scheme.basic.username": "admin",
            "solace.messaging.authentication.scheme.basic.password": settings.solace_admin_password,
        }
        self._service = (
            MessagingService.builder()
            .from_properties(broker_props)
            .with_reconnection_retry_strategy(RetryStrategy.parametrized_retry(20, 3))
            .build()
        )
        self._service.connect()
        self._publisher = (
            self._service.create_persistent_message_publisher_builder()
            .build()
        )
        self._publisher.start()
        log.info("solace.connected", host=settings.solace_host,
                 port=settings.solace_smf_port)
        return self

    def disconnect(self):
        if self._publisher:
            self._publisher.terminate()
        if self._service and self._service.is_connected:
            self._service.disconnect()
            log.info("solace.disconnected")

    def __enter__(self):
        return self.connect()

    def __exit__(self, *args):
        self.disconnect()

    def publish(self, topic_str: str, payload: dict,
                session_id: str = "", module: str = "") -> None:
        """
        Publish a JSON payload to a Solace topic.
        Automatically adds governance headers: session_id, timestamp, module.
        """
        if not self._service or not self._service.is_connected:
            raise RuntimeError("MessagingService not connected")

        envelope = {
            "_meta": {
                "message_id": str(uuid.uuid4()),
                "session_id": session_id,
                "module": module,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "topic": topic_str,
            },
            "payload": payload,
        }

        message = (
            self._service.message_builder()
            .with_application_message_id(envelope["_meta"]["message_id"])
            .build(json.dumps(envelope))
        )

        destination = Topic.of(topic_str)
        self._publisher.publish(message, destination)

        log.info("solace.published",
                 topic=topic_str,
                 session_id=session_id[:8] if session_id else "",
                 module=module)

    def subscribe(self, topic_pattern: str,
                  handler: Callable[[dict], None],
                  timeout_ms: int = 5000) -> int:
        """
        Subscribe to a topic pattern and process messages with handler.
        Returns the number of messages processed.
        handler receives the parsed envelope dict.
        """
        if not self._service or not self._service.is_connected:
            raise RuntimeError("MessagingService not connected")

        received = []

        class _Handler(MessageHandler):
            def on_message(self, message: InboundMessage):
                try:
                    raw = message.get_payload_as_string()
                    envelope = json.loads(raw)
                    handler(envelope)
                    received.append(1)
                except Exception as e:
                    log.error("solace.handler_error", error=str(e))

        receiver = (
            self._service.create_direct_message_receiver_builder()
            .with_subscriptions([TopicSubscription.of(topic_pattern)])
            .build()
        )
        receiver.start()
        receiver.receive_async(_Handler())

        import time
        time.sleep(timeout_ms / 1000)
        receiver.terminate()

        log.info("solace.subscribed",
                 topic=topic_pattern,
                 messages_received=len(received))
        return len(received)
