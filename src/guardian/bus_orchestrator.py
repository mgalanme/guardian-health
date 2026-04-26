"""
GUARDIAN-Health — Bus Orchestrator
Runs the three modules as decoupled producers and consumers
connected through Solace Agent Mesh.

Flow:
  VIGIL  -> publishes to guardian/v1/signals/raw/...
  ASSESS -> subscribes to signals/raw, publishes to signals/evaluated
  RESPOND-> subscribes to signals/evaluated, publishes to audit/...
  AUDIT  -> subscribes to guardian/v1/audit/> (cross-framework consumer)

Key design: each consumer signals a ready_event after calling receive_async().
The pipeline waits for ALL ready events before VIGIL publishes.
This guarantees subscriptions are active in the broker before any message fires.
"""

import json
import threading
import time
import uuid

import structlog
from solace.messaging.messaging_service import MessagingService
from solace.messaging.config.retry_strategy import RetryStrategy
from solace.messaging.resources.topic_subscription import TopicSubscription
from solace.messaging.receiver.message_receiver import MessageHandler
from solace.messaging.receiver.inbound_message import InboundMessage

from src.guardian.config import get_settings
from src.guardian.messaging import GuardianMessaging, Topics
from src.guardian.governance.audit import write_audit_record, ActionType, Module
from src.guardian.state import initial_state

log = structlog.get_logger(__name__)


# ── Solace connection helper ───────────────────────────────────────────────────

def _make_service() -> MessagingService:
    settings = get_settings()
    broker_props = {
        "solace.messaging.transport.host": f"tcp://{settings.solace_host}:{settings.solace_smf_port}",
        "solace.messaging.service.vpn-name": "default",
        "solace.messaging.authentication.scheme.basic.username": "admin",
        "solace.messaging.authentication.scheme.basic.password": settings.solace_admin_password,
    }
    service = (
        MessagingService.builder()
        .from_properties(broker_props)
        .with_reconnection_retry_strategy(RetryStrategy.parametrized_retry(20, 3))
        .build()
    )
    service.connect()
    log.info("solace.connected",
             host=settings.solace_host,
             port=settings.solace_smf_port)
    return service


# ── Producer: VIGIL ────────────────────────────────────────────────────────────

def vigil_producer(his_patient_id: str, centre_id: str = "centre-a") -> dict:
    """
    Run VIGIL for a patient and publish detected signals to Solace.
    Returns the VIGIL state for local inspection.
    """
    from src.modules.vigil.graph import run_vigil

    log.info("bus.vigil.start", his_id=his_patient_id)
    state = run_vigil(his_patient_id)

    if not state["raw_signals"]:
        log.info("bus.vigil.no_signals", his_id=his_patient_id)
        return state

    with GuardianMessaging() as msg:
        for i, signal in enumerate(state["raw_signals"]):
            topic = Topics.signal_raw(centre_id, state["patient_pseudo_id"])
            msg.publish(
                topic_str=topic,
                payload={
                    "his_patient_id": his_patient_id,
                    "patient_pseudo_id": state["patient_pseudo_id"],
                    "session_alias": state["session_alias"],
                    "session_id": state["session_id"],
                    "signal": signal,
                    "requires_hitl": state["requires_hitl"],
                    "vigil_assessment": (
                        state["messages"][-1].content[:300]
                        if state["messages"] else ""
                    ),
                },
                session_id=state["session_id"],
                module="VIGIL",
            )
            log.info("bus.vigil.published",
                     topic=topic,
                     pattern=signal["raw_data"].get("pattern"),
                     risk=signal["raw_data"].get("risk_level"))

        msg.publish(
            Topics.audit("VIGIL", "signals_published"),
            payload={"signals_count": len(state["raw_signals"]),
                     "session_id": state["session_id"]},
            session_id=state["session_id"],
            module="VIGIL",
        )

    return state


# ── Consumer + Producer: ASSESS ────────────────────────────────────────────────

def assess_consumer(ready_event: threading.Event,
                    evaluated: list,
                    wait_seconds: int = 180) -> None:
    """
    Subscribe to raw signals, run ASSESS for each, publish evaluations.
    Signals ready_event immediately after receive_async() so the pipeline
    knows the subscription is active in the broker before VIGIL publishes.
    Uses handler_done to wait for CrewAI crew to finish (30-60 seconds).
    """
    from src.modules.assess.crew import run_assess

    handler_done = threading.Event()
    signals_expected = [0]   # total signals in this batch (from signal_count in payload)
    signals_processed = [0]  # incremented after each signal is fully processed

    def handle_signal(envelope: dict):
        try:
            payload = envelope.get("payload", {})
            session_id = payload.get("session_id", str(uuid.uuid4()))
            pseudo_id = payload.get("patient_pseudo_id", "")
            session_alias = payload.get("session_alias", "")
            signal = payload.get("signal", {})

            total = payload.get("signal_count", 1)
            signals_expected[0] = total
            log.info("bus.assess.received",
                     session_id=session_id[:8],
                     pattern=signal.get("raw_data", {}).get("pattern", ""),
                     signal_index=payload.get("signal_index", 0),
                     signal_count=total)

            state = initial_state(
                patient_pseudo_id=pseudo_id,
                session_alias=session_alias,
                session_id=session_id,
                trace_id=f"bus-assess-{session_id[:8]}",
            )
            state["raw_signals"] = [signal]
            state["clinical_context"] = {
                "_patient_alias": session_alias,
                "_pseudo_id": pseudo_id,
                "_session_id": session_id,
                "_sanitised": True,
                "diagnoses": [],
                "medications": [],
                "gender": "unknown",
            }
            state["flow_status"] = "vigil_complete"

            assessed = run_assess(state)
            ev = assessed.get("evaluation")
            if not ev:
                return

            with GuardianMessaging() as msg:
                topic = Topics.signal_evaluated(ev["severity"], pseudo_id)
                msg.publish(
                    topic_str=topic,
                    payload={
                        "session_id": session_id,
                        "patient_pseudo_id": pseudo_id,
                        "session_alias": session_alias,
                        "evaluation": dict(ev),
                        "signal": signal,
                        "assessed_state": {
                            "flow_status": assessed["flow_status"],
                            "requires_hitl": assessed["requires_hitl"],
                        },
                    },
                    session_id=session_id,
                    module="ASSESS",
                )
                msg.publish(
                    Topics.audit("ASSESS", "evaluation_published"),
                    payload={"severity": ev["severity"],
                             "causality": ev["causality"],
                             "session_id": session_id},
                    session_id=session_id,
                    module="ASSESS",
                )
                log.info("bus.assess.published",
                         severity=ev["severity"],
                         causality=ev["causality"],
                         requires_hitl=ev["requires_hitl"])

            evaluated.append(assessed)

        except Exception as e:
            log.error("bus.assess.handler_error", error=str(e))
        finally:
            signals_processed[0] += 1
            if signals_expected[0] > 0 and signals_processed[0] >= signals_expected[0]:
                handler_done.set()
            elif signals_expected[0] == 0:
                handler_done.set()  # fallback: set if count unknown

    service = _make_service()

    class _Handler(MessageHandler):
        def on_message(self, message: InboundMessage):
            try:
                raw = message.get_payload_as_string()
                envelope = json.loads(raw)
                handle_signal(envelope)
            except Exception as e:
                log.error("solace.assess_handler_error", error=str(e))
                handler_done.set()

    receiver = (
        service.create_direct_message_receiver_builder()
        .with_subscriptions([TopicSubscription.of(Topics.SIGNALS_RAW_ALL)])
        .build()
    )
    receiver.start()
    receiver.receive_async(_Handler())

    # Signal that this consumer is subscribed and ready BEFORE waiting
    log.info("bus.assess.ready", topic=Topics.SIGNALS_RAW_ALL)
    ready_event.set()

    # Wait for handler to complete (CrewAI crew can take up to 60 seconds)
    handler_done.wait(timeout=wait_seconds)

    receiver.terminate()
    service.disconnect()
    log.info("bus.assess.done", results=len(evaluated))


# ── Consumer: RESPOND ──────────────────────────────────────────────────────────

def respond_consumer(ready_event: threading.Event,
                     final_states: list,
                     wait_seconds: int = 180) -> None:
    """
    Subscribe to evaluated signals, run RESPOND for each.
    Signals ready_event after receive_async() for the same reason as assess_consumer.
    """
    from src.modules.respond.coordinator import run_respond

    handler_done = threading.Event()

    def handle_evaluation(envelope: dict):
        try:
            payload = envelope.get("payload", {})
            session_id = payload.get("session_id", str(uuid.uuid4()))
            pseudo_id = payload.get("patient_pseudo_id", "")
            session_alias = payload.get("session_alias", "")
            ev = payload.get("evaluation", {})
            signal = payload.get("signal", {})

            log.info("bus.respond.received",
                     session_id=session_id[:8],
                     severity=ev.get("severity", ""))

            state = initial_state(
                patient_pseudo_id=pseudo_id,
                session_alias=session_alias,
                session_id=session_id,
                trace_id=f"bus-respond-{session_id[:8]}",
            )
            state["raw_signals"] = [signal]
            state["evaluation"] = ev
            state["requires_hitl"] = ev.get("requires_hitl", True)
            state["flow_status"] = "evaluated"

            final_state, notification = run_respond(state)

            with GuardianMessaging() as msg:
                msg.publish(
                    Topics.notification_clinician(session_id),
                    payload={"session_id": session_id,
                             "session_alias": session_alias,
                             "notification": notification,
                             "severity": ev.get("severity"),
                             "hitl_decision": dict(
                                 final_state.get("hitl_decision") or {})},
                    session_id=session_id,
                    module="RESPOND",
                )
                msg.publish(
                    Topics.audit("RESPOND", "session_complete"),
                    payload={"session_id": session_id,
                             "flow_status": final_state["flow_status"],
                             "hitl_decision": (
                                 final_state.get("hitl_decision") or {}
                             ).get("decision", "")},
                    session_id=session_id,
                    module="RESPOND",
                )
                log.info("bus.respond.complete",
                         session_id=session_id[:8],
                         flow_status=final_state["flow_status"])

            final_states.append(final_state)

        except Exception as e:
            log.error("bus.respond.handler_error", error=str(e))
        finally:
            handler_done.set()

    service = _make_service()

    class _Handler(MessageHandler):
        def on_message(self, message: InboundMessage):
            try:
                raw = message.get_payload_as_string()
                envelope = json.loads(raw)
                handle_evaluation(envelope)
            except Exception as e:
                log.error("solace.respond_handler_error", error=str(e))
                handler_done.set()

    receiver = (
        service.create_direct_message_receiver_builder()
        .with_subscriptions([TopicSubscription.of(Topics.SIGNALS_EVALUATED_ALL)])
        .build()
    )
    receiver.start()
    receiver.receive_async(_Handler())

    # Signal ready before waiting
    log.info("bus.respond.ready", topic=Topics.SIGNALS_EVALUATED_ALL)
    ready_event.set()

    handler_done.wait(timeout=wait_seconds)

    receiver.terminate()
    service.disconnect()
    log.info("bus.respond.done", results=len(final_states))


# ── Cross-framework audit consumer ─────────────────────────────────────────────

def audit_bus_consumer(ready_event: threading.Event,
                       pipeline_done: threading.Event,
                       bus_events: list,
                       wait_seconds: int = 180) -> None:
    """
    Cross-framework governance consumer.
    Subscribes to ALL audit topics regardless of which framework published them.
    Framework-agnostic: LangGraph, CrewAI, AutoGen, or direct API calls all
    produce events on the same audit topic hierarchy.
    Runs until pipeline_done is signalled, then collects for 3 more seconds.
    """
    def handle_audit_event(envelope: dict):
        meta = envelope.get("_meta", {})
        payload = envelope.get("payload", {})

        module_name = meta.get("module", "SYSTEM").upper()
        module = (Module[module_name]
                  if module_name in Module.__members__
                  else Module.SYSTEM)

        write_audit_record(
            session_id=meta.get("session_id", "bus-event"),
            module=module,
            agent_id=f"bus:{meta.get('topic', '')}",
            action_type=ActionType.STATE_TRANSITION,
            action_detail={
                "bus_topic": meta.get("topic", ""),
                "message_id": meta.get("message_id", ""),
                "source_module": meta.get("module", ""),
            },
            state_snapshot=payload,
        )
        log.info("bus.audit.recorded",
                 topic=meta.get("topic", ""),
                 module=meta.get("module", ""))
        bus_events.append(meta)

    service = _make_service()

    class _AuditHandler(MessageHandler):
        def on_message(self, message: InboundMessage):
            try:
                raw = message.get_payload_as_string()
                envelope = json.loads(raw)
                handle_audit_event(envelope)
            except Exception as e:
                log.error("solace.audit_handler_error", error=str(e))

    receiver = (
        service.create_direct_message_receiver_builder()
        .with_subscriptions([TopicSubscription.of(Topics.AUDIT_ALL)])
        .build()
    )
    receiver.start()
    receiver.receive_async(_AuditHandler())

    log.info("bus.audit.ready", topic=Topics.AUDIT_ALL)
    ready_event.set()

    # Run until pipeline completes, then linger for trailing events
    pipeline_done.wait(timeout=wait_seconds)
    time.sleep(3)

    receiver.terminate()
    service.disconnect()
    log.info("bus.audit.done", events_captured=len(bus_events))


# ── Full decoupled pipeline ────────────────────────────────────────────────────

def run_decoupled_pipeline(his_patient_id: str,
                            centre_id: str = "centre-a") -> dict:
    """
    Run the full pipeline in decoupled mode using Solace as the integration bus.

    Correct event-driven sequence:
      1. Start consumers in parallel threads
      2. Each consumer signals ready_event after receive_async()
      3. Main thread waits for ALL ready events (not a fixed sleep)
      4. VIGIL publishes — all consumers are guaranteed to be subscribed
      5. ASSESS processes signal, publishes evaluation
      6. RESPOND processes evaluation, publishes notification
      7. Audit consumer captures all cross-framework events
    """
    log.info("bus.pipeline.start", his_id=his_patient_id)
    start = time.time()

    assessed_states = []
    final_states = []
    bus_events = []

    assess_ready   = threading.Event()
    respond_ready  = threading.Event()
    audit_ready    = threading.Event()
    pipeline_done  = threading.Event()

    def run_assess_thread():
        assess_consumer(assess_ready, assessed_states)

    def run_respond_thread():
        respond_consumer(respond_ready, final_states)

    def run_audit_thread():
        audit_bus_consumer(audit_ready, pipeline_done, bus_events)

    # Pre-import heavy modules in the main thread before spawning threads.
    # CrewAI and LangGraph do thread-unsafe initialisation on first import.
    # Once in sys.modules, secondary thread imports are a safe no-op.
    from src.modules.assess.crew import run_assess        # noqa: F401
    from src.modules.respond.coordinator import run_respond  # noqa: F401
    from src.modules.vigil.graph import run_vigil          # noqa: F401

    t_assess  = threading.Thread(target=run_assess_thread, daemon=True)
    t_respond = threading.Thread(target=run_respond_thread, daemon=True)
    t_audit   = threading.Thread(target=run_audit_thread, daemon=True)

    t_assess.start()
    t_respond.start()
    t_audit.start()

    # Wait for all three consumers to confirm they are subscribed
    log.info("bus.pipeline.waiting_for_consumers")
    assess_ready.wait(timeout=30)
    respond_ready.wait(timeout=30)
    audit_ready.wait(timeout=30)
    log.info("bus.pipeline.all_consumers_ready")

    # Small buffer to ensure broker has propagated subscriptions
    time.sleep(1)

    # VIGIL produces — all consumers are confirmed subscribed
    vigil_state = vigil_producer(his_patient_id, centre_id)
    if not vigil_state["raw_signals"]:
        pipeline_done.set()
        return {"status": "no_signals", "his_id": his_patient_id}

    log.info("bus.pipeline.vigil_published",
             signals=len(vigil_state["raw_signals"]))

    # Wait for ASSESS and RESPOND to complete
    t_assess.join(timeout=180)
    t_respond.join(timeout=180)

    # Signal audit consumer that pipeline is done, then wait for it
    pipeline_done.set()
    t_audit.join(timeout=15)

    duration = round(time.time() - start, 2)

    result = {
        "his_id": his_patient_id,
        "duration_seconds": duration,
        "signals_on_bus": len(vigil_state["raw_signals"]),
        "assessments_completed": len(assessed_states),
        "responses_completed": len(final_states),
        "bus_audit_events": len(bus_events),
        "final_status": (final_states[0]["flow_status"]
                         if final_states else "pending"),
        "severity": (assessed_states[0]["evaluation"]["severity"]
                     if assessed_states and assessed_states[0].get("evaluation")
                     else "unknown"),
    }

    log.info("bus.pipeline.complete", **result)
    return result
