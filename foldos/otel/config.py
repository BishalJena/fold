from __future__ import annotations

from dataclasses import dataclass

from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, LogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter

_DEFAULT_RESOURCE = Resource.create({"service.name": "foldos"})


@dataclass
class OtelProviders:
    resource: Resource
    tracer_provider: TracerProvider
    meter_provider: MeterProvider
    logger_provider: LoggerProvider
    span_processor: SpanProcessor | None = None
    log_processor: LogRecordProcessor | None = None
    metric_reader: MetricReader | None = None

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        results: list[bool] = []
        if self.tracer_provider is not None:
            results.append(self.tracer_provider.force_flush(timeout_millis=timeout_millis))
        if self.logger_provider is not None:
            results.append(self.logger_provider.force_flush(timeout_millis=timeout_millis))
        if self.metric_reader is not None:
            results.append(self.metric_reader.force_flush(timeout_millis=timeout_millis))
        return all(results)

    def shutdown(self) -> None:
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()
        if self.meter_provider is not None:
            self.meter_provider.shutdown()
        if self.logger_provider is not None:
            self.logger_provider.shutdown()  # type: ignore[no-untyped-call]


def build_providers(
    resource: Resource | None = None,
    *,
    span_exporter: SpanExporter | None = None,
    metric_reader: MetricReader | None = None,
    log_exporter: LogRecordExporter | None = None,
) -> OtelProviders:
    if resource is None:
        resource = _DEFAULT_RESOURCE

    if span_exporter is None:
        span_processor: SpanProcessor = BatchSpanProcessor(OTLPSpanExporter())
    else:
        span_processor = SimpleSpanProcessor(span_exporter)

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(span_processor)

    if metric_reader is None:
        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())

    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])

    if log_exporter is None:
        log_processor: LogRecordProcessor = BatchLogRecordProcessor(OTLPLogExporter())
    else:
        log_processor = SimpleLogRecordProcessor(log_exporter)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(log_processor)

    return OtelProviders(
        resource=resource,
        tracer_provider=tracer_provider,
        meter_provider=meter_provider,
        logger_provider=logger_provider,
        span_processor=span_processor,
        log_processor=log_processor,
        metric_reader=metric_reader,
    )
