"""Optional, instance-local OpenTelemetry export to Langfuse."""

import base64
import json
import logging
from collections.abc import Mapping

from .masking import mask_secrets

logger = logging.getLogger(__name__)


class LangfuseExporter:
    """Export turns without replacing the application's global tracer provider."""

    def __init__(self, public_key: str, secret_key: str, host: str):
        self.public_key = public_key
        self.secret_key = secret_key
        self.host = host.rstrip("/")
        self._provider = None
        self._tracer = None

    def setup(self) -> bool:
        """Configure OTLP/HTTP. Return False if dependencies/config are missing."""
        if self._tracer is not None:
            return True
        if not self.public_key or not self.secret_key or not self.host:
            return False
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            logger.info("Langfuse disabled: install OpenTelemetry OTLP dependencies")
            return False

        provider = None
        try:
            token = base64.b64encode(f"{self.public_key}:{self.secret_key}".encode()).decode(
                "ascii"
            )
            exporter = OTLPSpanExporter(
                endpoint=f"{self.host}/api/public/otel/v1/traces",
                headers={"Authorization": f"Basic {token}"},
                timeout=10,
            )
            provider = TracerProvider(resource=Resource.create({"service.name": "stiracore"}))
            provider.add_span_processor(BatchSpanProcessor(exporter))
            self._tracer = provider.get_tracer(__name__)
            self._provider = provider
            return True
        except Exception:  # noqa: BLE001 - isolate optional plugin or batch-job failures
            if provider is not None:
                provider.shutdown()
            logger.warning("Langfuse exporter could not be configured")
            return False

    def trace_turn(self, turn_id: str, events) -> str:
        """Return a 32-character trace ID, or '' when exporting is disabled.

        Events are mappings with optional ``name``/``type`` fields, or strings.
        Callers should redact event data; it is masked again here (#185) so an
        export never carries credentials even if a caller forgets.
        """
        if self._tracer is None and not self.setup():
            return ""
        from opentelemetry.context import Context

        with self._tracer.start_as_current_span(
            "agent.turn",
            context=Context(),
            attributes={"turn.id": str(turn_id)},
        ) as span:
            for event in events:
                data = dict(event) if isinstance(event, Mapping) else {"message": str(event)}
                data = mask_secrets(data)
                name = str(data.pop("name", data.pop("type", "event")))
                attributes = {}
                for key, value in data.items():
                    if value is None:
                        continue
                    attributes[str(key)] = (
                        value
                        if isinstance(value, (str, bool, int, float))
                        else mask_secrets(json.dumps(value, default=str, ensure_ascii=False))
                    )
                span.add_event(name, attributes=attributes)
            return f"{span.get_span_context().trace_id:032x}"

    def shutdown(self) -> None:
        """Flush queued spans and release exporter resources."""
        provider, self._provider = self._provider, None
        self._tracer = None
        if provider is not None:
            provider.shutdown()
