from __future__ import annotations

from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from foldos.otel.config import build_providers


def test_build_providers_shares_the_supplied_resource() -> None:
    resource = Resource.create({"service.name": "test"})
    providers = build_providers(resource=resource)
    assert providers.resource is resource
    providers.shutdown()


def test_build_providers_is_injectable_for_tests() -> None:
    span_exporter = InMemorySpanExporter()
    log_exporter = InMemoryLogRecordExporter()
    metric_reader = InMemoryMetricReader()
    providers = build_providers(
        span_exporter=span_exporter,
        log_exporter=log_exporter,
        metric_reader=metric_reader,
    )
    assert isinstance(providers.span_processor, SimpleSpanProcessor)
    assert isinstance(providers.log_processor, SimpleLogRecordProcessor)
    assert providers.metric_reader is metric_reader
    assert providers.force_flush()
    providers.shutdown()


def test_build_providers_keeps_batch_defaults() -> None:
    providers = build_providers()
    assert isinstance(providers.span_processor, BatchSpanProcessor)
    providers.shutdown()
