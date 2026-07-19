"""OpenTelemetry tracing for idc-migrate — file-exported, env-gated.

Writes finished spans as one JSON line each to ``IDC_TRACE_FILE`` (default
empty = fully OFF, so the test suite and idle runs pay zero cost). Covers all
three signals the operator asked for:

  * FastAPI HTTP requests — every ``/api/...`` route, latency + status code.
  * PyMySQL DB queries — every call against the MariaDB on 10.0.0.3, with the
    statement as a span attribute.
  * Outbound httpx — the LLM calls to the Ollama gateway and the pull-mode
    executor context pulls.

The JSONL shape mirrors the OTLP Span proto closely enough that a downstream
collector (Filebeat / Vector / the OTel collector with the filelog receiver,
or a tiny replay script) can re-ingest it later. We deliberately do NOT use the
OTLP HTTP exporter because the operator wants file output for now and will
wire collection separately.

Live enablement is via a systemd drop-in (see ``scripts/tracing.conf``):
    Environment=IDC_TRACE_FILE=/var/log/idc-migrate/traces.jsonl
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# Heavy imports are deferred to setup_tracing() so importing this module is
# cheap when tracing is OFF (the common path in tests).

_DEFAULT_SERVICE_NAME = "idc-migrate"
_initialized = False
_lock = threading.Lock()


class _JSONLineSpanExporter:
    """SpanExporter that appends one JSON object per finished span to a file.

    Implements the duck-typed surface used by BatchSpanProcessor:
    ``export(spans) -> SpanExportResult``, ``shutdown()``, ``force_flush()``.
    We hand-roll serialization rather than depending on the OTLP JSON encoder
    so the output stays stable and self-describing for later collection.
    """

    def __init__(self, path: str, service_name: str) -> None:
        self._path = Path(path)
        self._service_name = service_name
        # Best-effort parent dir create (e.g. /var/log/idc-migrate may not
        # exist yet). If the path itself is unwritable we raise — better a
        # loud boot failure than silent tracing-off.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._f = open(self._path, "a", encoding="utf-8")

    # -- SpanExporter protocol -------------------------------------------
    def export(self, spans: Any) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult
        lines = []
        for s in spans:
            try:
                lines.append(json.dumps(_serialize_span(s, self._service_name),
                                        default=_json_default))
            except Exception:  # never let tracing take the app down
                continue
        if lines:
            with self._lock:
                self._f.write("".join(line + "\n" for line in lines))
                self._f.flush()
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        with self._lock:
            try:
                self._f.close()
            except Exception:
                pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        with self._lock:
            try:
                self._f.flush()
            except Exception:
                return False
        return True


def _json_default(obj: Any) -> Any:
    # opentelemetry attributes can be bytes / enums / tuples — coerce to str.
    if isinstance(obj, bytes):
        try:
            return obj.decode("utf-8")
        except UnicodeDecodeError:
            return obj.decode("latin-1", "replace")
    if hasattr(obj, "name"):  # enum-like
        return obj.name
    if hasattr(obj, "__int__"):
        return int(obj)
    return str(obj)


def _hex(val: Optional[int], width: int) -> Optional[str]:
    if not val:
        return None
    return f"{val:0{width}x}"


def _serialize_span(span: Any, service_name: str) -> Dict[str, Any]:
    ctx = span.get_span_context()
    parent = getattr(span, "parent", None)
    status = span.status
    rec = {
        "resource": {"service.name": service_name},
        "trace_id": _hex(ctx.trace_id, 32) if ctx else None,
        "span_id": _hex(ctx.span_id, 16) if ctx else None,
        "parent_span_id": _hex(parent.span_id, 16) if parent and parent.span_id else None,
        "name": span.name,
        "kind": str(span.kind),
        "start_time_unix_nano": span.start_time,
        "end_time_unix_nano": span.end_time,
        "status_code": str(status.status_code) if status else "UNSET",
        "status_description": status.description if status else "",
        "attributes": dict(span.attributes or {}),
    }
    # events (exceptions etc.) — keep light
    events = getattr(span, "events", None) or []
    if events:
        rec["events"] = [
            {"name": e.name,
             "timestamp": e.timestamp,
             "attributes": dict(e.attributes or {})}
            for e in events
        ]
    return rec


def setup_tracing(settings: Any) -> bool:
    """Install the TracerProvider + instrumentations. Idempotent + OFF-safe.

    Returns True if tracing was enabled, False if it stayed off (no
    IDC_TRACE_FILE). Called once at backend startup from ``app.py`` BEFORE
    the FastAPI app is built and BEFORE ``open_store`` runs so PyMySQL
    instrumentation wraps the first connection.
    """
    global _initialized
    with _lock:
        if _initialized:
            return True
        path = (getattr(settings, "trace_file", "") or "").strip()
        if not path:
            return False
        # Import lazily so a missing/wrong opentelemetry install never breaks
        # an untraced run (and tests don't pay the import cost).
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        service_name = (getattr(settings, "trace_service_name", "") or "").strip() \
            or _DEFAULT_SERVICE_NAME
        resource = Resource.create({SERVICE_NAME: service_name})
        provider = TracerProvider(resource=resource)
        exporter = _JSONLineSpanExporter(path, service_name)
        provider.add_span_processor(BatchSpanProcessor(
            exporter,
            # Flush fairly promptly so the file is a near-live perf log, not
            # a delayed buffer; small batch since this box is low-traffic.
            max_queue_size=1024,
            schedule_delay_millis=2000,
            max_export_batch_size=64,
        ))
        trace.set_tracer_provider(provider)

        # Instrument httpx + PyMySQL globally — must happen before the first
        # httpx/PyMySQL call (the LLM client + open_store both run at module
        # import time, so we call this BEFORE they're constructed in app.py).
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        HTTPXClientInstrumentor().instrument()
        try:
            from opentelemetry.instrumentation.pymysql import PyMySQLInstrumentor
            PyMySQLInstrumentor().instrument()
        except Exception:
            # PyMySQL instrumentation is optional at runtime — if the install
            # is partial we still get HTTP + httpx spans.
            pass

        _initialized = True
        return True


def instrument_app(app: Any) -> None:
    """Instrument a FastAPI app. No-op if tracing is OFF."""
    if not _initialized:
        return
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    FastAPIInstrumentor.instrument_app(app)