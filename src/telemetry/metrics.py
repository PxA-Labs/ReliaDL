"""
Prometheus metrics registry and exposition server for ReliaDL.

Implements the three metric types a transfer needs, the text exposition format a
scraper reads, and a background HTTP server to serve it from.

Why this is hand-written
------------------------
The format is a short, stable, well-specified grammar, and implementing it here
keeps the dependency list at five packages rather than six for a few hundred
lines. It also keeps the hot path visible: an observation happens once per
chunk on the transfer path, and a library whose costs are not obvious is a poor
place to discover a per-observation allocation.

What the exposition format demands
----------------------------------
Three details are easy to get wrong and each breaks the scrape rather than
degrading it.

Histogram buckets are **cumulative**. A bucket labelled ``le="0.5"`` counts
every observation at or below 0.5, not those falling between it and the
previous bound. Emitting disjoint counts produces a histogram that parses
cleanly and yields nonsense quantiles, which is worse than failing. The
``+Inf`` bucket is mandatory and always equals the observation count.

Counters must only ever increase. Prometheus reads a decrease as a process
restart and, for ``rate()``, treats the gap as a counter reset — producing an
enormous spurious spike rather than the negative rate that would be obviously
wrong. A decrement is therefore refused here rather than recorded.

Help text and label values need escaping, and by different rules: help text
escapes backslashes and newlines, label values escape backslashes, newlines and
double quotes. An unescaped quote in a label truncates the sample and corrupts
everything after it on that line.

Bucket scales
-------------
Durations and throughputs are given separate defaults because they differ by
nine orders of magnitude. Duration bounds run from a few milliseconds to a
minute, since a chunk that takes longer has failed rather than been slow.
Throughput bounds are geometric from a kilobyte per second to a gigabyte per
second, because the interesting question about a transfer rate is which decade
it is in, not its linear position.
"""

from __future__ import annotations

import bisect
import math
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.exceptions import ConfigurationError

# Content type a Prometheus scraper expects for the text exposition format.
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Path the registry is exposed on.
METRICS_PATH = "/metrics"

# Chunk durations, in seconds. Bounded at a minute: a chunk slower than that has
# failed rather than been slow, and the straggler scheduler will have re-issued
# it long before.
DEFAULT_DURATION_BUCKETS: Tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
)

# Throughput, in bytes per second, geometric from 1 KB/s to 1 GB/s. The useful
# question about a transfer rate is which decade it occupies.
DEFAULT_THROUGHPUT_BUCKETS: Tuple[float, ...] = (
    1024.0,
    10 * 1024.0,
    100 * 1024.0,
    1024.0 ** 2,
    5 * 1024.0 ** 2,
    10 * 1024.0 ** 2,
    50 * 1024.0 ** 2,
    100 * 1024.0 ** 2,
    500 * 1024.0 ** 2,
    1024.0 ** 3,
)

# Metric and label names Prometheus accepts.
_NAME_PATTERN = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_LABEL_PATTERN = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _validate_name(name: str, pattern: re.Pattern, kind: str) -> str:
    """
    Validate a metric or label name against the exposition grammar.

    Raises:
        ConfigurationError: If the name is empty or not a legal identifier.
    """
    if not isinstance(name, str) or not name:
        raise ConfigurationError(
            f"{kind} name must be a non-empty string",
            parameter=kind,
            value=name,
        )
    if not pattern.match(name):
        raise ConfigurationError(
            f"{kind} name {name!r} is not a valid Prometheus identifier; a "
            "scraper rejects the whole payload rather than the offending line",
            parameter=kind,
            value=name,
        )
    return name


def escape_help(text: str) -> str:
    """
    Escape help text for the exposition format.

    Backslashes and newlines only. A newline would terminate the HELP line and
    leave the remainder to be parsed as a metric.
    """
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def escape_label_value(value: str) -> str:
    """
    Escape a label value for the exposition format.

    Backslashes, newlines and double quotes. An unescaped quote closes the value
    early and corrupts everything after it on that line, so the sample is not
    merely wrong but takes its neighbours with it.
    """
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def format_value(value: float) -> str:
    """
    Render a float in the form the exposition format expects.

    Infinities are spelled ``+Inf`` and ``-Inf`` rather than Python's ``inf``,
    and integral values are emitted without a trailing ``.0`` to keep payloads
    small on counters, which are the overwhelming majority of samples.
    """
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == int(value) and abs(value) < 1e16:
        return str(int(value))
    return repr(value)


@dataclass(frozen=True)
class Sample:
    """
    One rendered time series.

    Attributes:
        name: Series name, which for histograms differs from the metric name.
        labels: Label set identifying the series.
        value: Current value.
    """

    name: str
    labels: Tuple[Tuple[str, str], ...]
    value: float

    def render(self) -> str:
        """Render this sample as an exposition line."""
        if not self.labels:
            return f"{self.name} {format_value(self.value)}"
        rendered = ",".join(
            f'{key}="{escape_label_value(value)}"' for key, value in self.labels
        )
        return f"{self.name}{{{rendered}}} {format_value(self.value)}"


class Metric:
    """
    Base class holding a metric's identity and its per-label-set series.

    Series are created on first use rather than declared, which matches how a
    transfer discovers its own label values — a mirror hostname is not known
    until a mirror is chosen.
    """

    metric_type = "untyped"

    def __init__(
        self,
        name: str,
        documentation: str = "",
        label_names: Sequence[str] = (),
    ) -> None:
        self.name = _validate_name(name, _NAME_PATTERN, "metric")
        self.documentation = documentation
        names = tuple(label_names)
        for label in names:
            _validate_name(label, _LABEL_PATTERN, "label")
        if len(set(names)) != len(names):
            raise ConfigurationError(
                f"Duplicate label name in {names}",
                parameter="label_names",
                value=names,
            )
        self.label_names = names
        self._lock = threading.Lock()

    def _key(self, labels: Mapping[str, str]) -> Tuple[Tuple[str, str], ...]:
        """
        Normalize a label mapping into a stable series key.

        Ordered by the declared label names rather than by the caller's keyword
        order, so the same series is not split in two by argument order.

        Raises:
            ConfigurationError: If the labels do not match the declared names.
        """
        if set(labels) != set(self.label_names):
            raise ConfigurationError(
                f"Metric {self.name!r} declares labels {list(self.label_names)} "
                f"but was given {sorted(labels)}; a partial label set would "
                "create a separate series rather than updating the intended one",
                parameter="labels",
                value=sorted(labels),
            )
        return tuple((name, str(labels[name])) for name in self.label_names)

    def collect(self) -> List[Sample]:
        """Produce the samples for this metric."""
        raise NotImplementedError

    def render(self) -> str:
        """Render this metric's HELP, TYPE and samples."""
        lines = []
        if self.documentation:
            lines.append(f"# HELP {self.name} {escape_help(self.documentation)}")
        lines.append(f"# TYPE {self.name} {self.metric_type}")
        lines.extend(sample.render() for sample in self.collect())
        return "\n".join(lines)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(name={self.name!r})"


class Counter(Metric):
    """
    A monotonically increasing total.

    Decrements are refused rather than recorded. Prometheus interprets a falling
    counter as a process restart, so ``rate()`` over the gap produces an
    enormous spurious spike instead of the obviously-wrong negative rate that
    would at least be noticed.
    """

    metric_type = "counter"

    def __init__(self, name: str, documentation: str = "", label_names: Sequence[str] = ()) -> None:
        super().__init__(name, documentation, label_names)
        self._values: Dict[Tuple[Tuple[str, str], ...], float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """
        Add to the counter.

        Raises:
            ConfigurationError: If the amount is negative or not finite.
        """
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise ConfigurationError(
                f"amount must be numeric, got {type(amount).__name__}",
                parameter="amount",
                value=amount,
            )
        if not math.isfinite(amount):
            raise ConfigurationError(
                f"amount must be finite, got {amount}",
                parameter="amount",
                value=amount,
            )
        if amount < 0:
            raise ConfigurationError(
                f"Counter {self.name!r} cannot decrease (got {amount}). "
                "Prometheus reads a falling counter as a restart and turns the "
                "gap into a spurious rate spike; use a Gauge for a value that "
                "goes down",
                parameter="amount",
                value=amount,
            )
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def value(self, **labels: str) -> float:
        """Current total for one series."""
        key = self._key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def collect(self) -> List[Sample]:
        """Produce one sample per label set."""
        with self._lock:
            items = list(self._values.items())
        return [Sample(self.name, key, value) for key, value in items]


class Gauge(Metric):
    """A value that may move in either direction."""

    metric_type = "gauge"

    def __init__(self, name: str, documentation: str = "", label_names: Sequence[str] = ()) -> None:
        super().__init__(name, documentation, label_names)
        self._values: Dict[Tuple[Tuple[str, str], ...], float] = {}

    def set(self, value: float, **labels: str) -> None:
        """
        Set the gauge.

        Raises:
            ConfigurationError: If the value is not a finite number.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigurationError(
                f"value must be numeric, got {type(value).__name__}",
                parameter="value",
                value=value,
            )
        if not math.isfinite(value):
            raise ConfigurationError(
                f"value must be finite, got {value}",
                parameter="value",
                value=value,
            )
        key = self._key(labels)
        with self._lock:
            self._values[key] = float(value)

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        """Add to the gauge."""
        key = self._key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(amount)

    def dec(self, amount: float = 1.0, **labels: str) -> None:
        """Subtract from the gauge."""
        self.inc(-amount, **labels)

    def value(self, **labels: str) -> float:
        """Current value for one series."""
        key = self._key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def collect(self) -> List[Sample]:
        """Produce one sample per label set."""
        with self._lock:
            items = list(self._values.items())
        return [Sample(self.name, key, value) for key, value in items]


@dataclass
class _HistogramSeries:
    """Bucket counts, running sum and observation count for one label set."""

    counts: List[int]
    total: float = 0.0
    count: int = 0


class Histogram(Metric):
    """
    Bucketed observations, rendered as cumulative counts.

    Bucket selection is a binary search rather than a scan. It matters little at
    thirteen bounds, but observation sits on the transfer path and the cost
    should not grow with however many bounds an operator decides to configure.
    """

    metric_type = "histogram"

    def __init__(
        self,
        name: str,
        documentation: str = "",
        buckets: Sequence[float] = DEFAULT_DURATION_BUCKETS,
        label_names: Sequence[str] = (),
    ) -> None:
        super().__init__(name, documentation, label_names)
        bounds = [float(bound) for bound in buckets if not math.isinf(bound)]
        if not bounds:
            raise ConfigurationError(
                "At least one finite bucket bound is required",
                parameter="buckets",
                value=buckets,
            )
        if any(not math.isfinite(bound) for bound in bounds):
            raise ConfigurationError(
                "Bucket bounds must be finite numbers",
                parameter="buckets",
                value=buckets,
            )
        if list(bounds) != sorted(bounds):
            raise ConfigurationError(
                f"Bucket bounds must be sorted ascending, got {buckets}. "
                "Cumulative counts are meaningless otherwise",
                parameter="buckets",
                value=buckets,
            )
        if len(set(bounds)) != len(bounds):
            raise ConfigurationError(
                f"Bucket bounds must be distinct, got {buckets}",
                parameter="buckets",
                value=buckets,
            )
        self.bounds: Tuple[float, ...] = tuple(bounds)
        self._series: Dict[Tuple[Tuple[str, str], ...], _HistogramSeries] = {}

    def observe(self, value: float, **labels: str) -> None:
        """
        Record one observation.

        Raises:
            ConfigurationError: If the value is not a finite number.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigurationError(
                f"value must be numeric, got {type(value).__name__}",
                parameter="value",
                value=value,
            )
        if not math.isfinite(value):
            raise ConfigurationError(
                f"value must be finite, got {value}",
                parameter="value",
                value=value,
            )
        key = self._key(labels)
        # bisect_left, not bisect_right: le="bound" means "at or below", so a
        # value sitting exactly on a bound belongs to that bucket rather than
        # the next one up. bisect_right would push it past, and the two agree
        # everywhere except on the bounds themselves — which is precisely where
        # a histogram's bucket edges are checked.
        index = bisect.bisect_left(self.bounds, float(value))
        with self._lock:
            series = self._series.get(key)
            if series is None:
                series = _HistogramSeries(counts=[0] * (len(self.bounds) + 1))
                self._series[key] = series
            series.counts[index] += 1
            series.total += float(value)
            series.count += 1

    def sample_count(self, **labels: str) -> int:
        """Number of observations recorded for one series."""
        key = self._key(labels)
        with self._lock:
            series = self._series.get(key)
            return series.count if series else 0

    def sample_sum(self, **labels: str) -> float:
        """Sum of observed values for one series."""
        key = self._key(labels)
        with self._lock:
            series = self._series.get(key)
            return series.total if series else 0.0

    def bucket_counts(self, **labels: str) -> List[int]:
        """
        Cumulative counts, one per bound plus the ``+Inf`` bucket.

        Cumulative because that is what the format means: a bucket counts every
        observation at or below its bound, not those between it and the
        previous one.
        """
        key = self._key(labels)
        with self._lock:
            series = self._series.get(key)
            raw = list(series.counts) if series else [0] * (len(self.bounds) + 1)
        cumulative = []
        running = 0
        for value in raw:
            running += value
            cumulative.append(running)
        return cumulative

    def collect(self) -> List[Sample]:
        """Produce bucket, sum and count samples for every label set."""
        with self._lock:
            keys = list(self._series)
        samples: List[Sample] = []
        for key in keys:
            cumulative = self.bucket_counts(**dict(key))
            for bound, total in zip(self.bounds, cumulative):
                samples.append(
                    Sample(
                        f"{self.name}_bucket",
                        key + (("le", format_value(bound)),),
                        total,
                    )
                )
            samples.append(
                Sample(
                    f"{self.name}_bucket",
                    key + (("le", "+Inf"),),
                    cumulative[-1],
                )
            )
            samples.append(
                Sample(f"{self.name}_sum", key, self.sample_sum(**dict(key)))
            )
            samples.append(
                Sample(f"{self.name}_count", key, self.sample_count(**dict(key)))
            )
        return samples


class MetricsRegistry:
    """
    Holds the metrics a process exposes and renders them for a scrape.

    Registration is explicit and duplicate names are refused: two metrics
    sharing a name produce a payload with two HELP lines for it, which a scraper
    rejects outright.
    """

    def __init__(self) -> None:
        self._metrics: Dict[str, Metric] = {}
        self._lock = threading.Lock()

    def register(self, metric: Metric) -> Metric:
        """
        Add a metric to the registry.

        Raises:
            ConfigurationError: If the name is already registered.
        """
        if not isinstance(metric, Metric):
            raise ConfigurationError(
                f"Only Metric instances can be registered, got {type(metric).__name__}",
                parameter="metric",
                value=type(metric).__name__,
            )
        with self._lock:
            if metric.name in self._metrics:
                raise ConfigurationError(
                    f"A metric named {metric.name!r} is already registered; a "
                    "duplicate produces two HELP lines and the scraper rejects "
                    "the whole payload",
                    parameter="metric",
                    value=metric.name,
                )
            self._metrics[metric.name] = metric
        return metric

    def unregister(self, name: str) -> None:
        """Remove a metric by name, if present."""
        with self._lock:
            self._metrics.pop(name, None)

    def get(self, name: str) -> Optional[Metric]:
        """Look up a registered metric."""
        with self._lock:
            return self._metrics.get(name)

    @property
    def names(self) -> Tuple[str, ...]:
        """Registered metric names, in registration order."""
        with self._lock:
            return tuple(self._metrics)

    def render(self) -> str:
        """
        Render the whole registry in the text exposition format.

        Ends with a newline, which the format requires: a payload whose final
        sample is unterminated is treated as truncated.
        """
        with self._lock:
            metrics = list(self._metrics.values())
        blocks = [metric.render() for metric in metrics]
        return "\n".join(block for block in blocks if block) + "\n"

    def __repr__(self) -> str:
        return f"MetricsRegistry(metrics={len(self._metrics)})"


class DownloadMetrics:
    """
    The catalog of metrics a ReliaDL transfer exposes.

    A named facade over the registry so the engine records outcomes through
    methods rather than by reaching for metric objects. That keeps the metric
    names in one place: a typo in a name is invisible until someone notices a
    dashboard has been empty for a month.
    """

    def __init__(
        self,
        registry: Optional[MetricsRegistry] = None,
        namespace: str = "chunkguard",
        label_names: Sequence[str] = (),
    ) -> None:
        self.registry = registry if registry is not None else MetricsRegistry()
        self.namespace = _validate_name(namespace, _NAME_PATTERN, "namespace")
        labels = tuple(label_names)

        self.bytes_downloaded = self.registry.register(
            Counter(
                f"{namespace}_bytes_downloaded_total",
                "Total bytes successfully downloaded.",
                labels,
            )
        )
        self.chunk_retries = self.registry.register(
            Counter(
                f"{namespace}_chunk_retries_total",
                "Total chunk download attempts that were retried.",
                labels,
            )
        )
        self.chunk_duration = self.registry.register(
            Histogram(
                f"{namespace}_chunk_download_duration_seconds",
                "Wall-clock duration of individual chunk downloads.",
                DEFAULT_DURATION_BUCKETS,
                labels,
            )
        )
        self.chunk_throughput = self.registry.register(
            Histogram(
                f"{namespace}_chunk_throughput_bytes_per_second",
                "Observed throughput of individual chunk downloads.",
                DEFAULT_THROUGHPUT_BUCKETS,
                labels,
            )
        )
        self.active_workers = self.registry.register(
            Gauge(
                f"{namespace}_active_workers",
                "Workers currently transferring a chunk.",
                labels,
            )
        )
        self.progress_ratio = self.registry.register(
            Gauge(
                f"{namespace}_download_progress_ratio",
                "Fraction of the transfer completed, from 0 to 1.",
                labels,
            )
        )

    def record_chunk(
        self, bytes_transferred: int, duration_seconds: float, **labels: str
    ) -> None:
        """
        Record one completed chunk across every metric it affects.

        Throughput is derived here rather than asked for, so the two histograms
        cannot disagree about the same transfer. A zero or negative duration
        contributes bytes and duration but no throughput, since dividing by it
        would report an infinite rate that lands in the top bucket and stays
        there.
        """
        self.bytes_downloaded.inc(bytes_transferred, **labels)
        if duration_seconds > 0:
            self.chunk_duration.observe(duration_seconds, **labels)
            self.chunk_throughput.observe(bytes_transferred / duration_seconds, **labels)

    def record_retry(self, count: int = 1, **labels: str) -> None:
        """Record one or more retried chunk attempts."""
        self.chunk_retries.inc(count, **labels)

    def set_active_workers(self, count: int, **labels: str) -> None:
        """Report how many workers are currently transferring."""
        self.active_workers.set(count, **labels)

    def set_progress(self, completed_bytes: int, total_bytes: int, **labels: str) -> None:
        """
        Report transfer progress as a ratio.

        A zero total reports zero rather than dividing: a transfer whose size is
        not yet known has made no measurable progress, and NaN in a gauge breaks
        arithmetic in every dashboard that touches it.
        """
        ratio = 0.0 if total_bytes <= 0 else completed_bytes / total_bytes
        self.progress_ratio.set(min(1.0, max(0.0, ratio)), **labels)

    def render(self) -> str:
        """Render the underlying registry."""
        return self.registry.render()

    def __repr__(self) -> str:
        return f"DownloadMetrics(namespace={self.namespace!r})"


class _MetricsHandler(BaseHTTPRequestHandler):
    """Serves the registry on /metrics and nothing else anywhere else."""

    # Set by the server when the handler class is created.
    registry: MetricsRegistry

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
        """Serve the exposition payload, or 404."""
        path = self.path.split("?", 1)[0]
        if path != METRICS_PATH:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"404: try " + METRICS_PATH.encode("ascii") + b"\n")
            return

        payload = self.registry.render().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", CONTENT_TYPE)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        """
        Discard the default request log.

        BaseHTTPRequestHandler writes a line to stderr per request. A scraper
        polls every few seconds for the life of the process, which would bury
        the transfer's own structured logs in noise.
        """


class MetricsServer:
    """
    Background HTTP server exposing the registry to a scraper.

    Runs on a daemon thread so a finished transfer exits without waiting for a
    scrape that may never come, and binds on construction so a port conflict is
    raised to the caller rather than lost inside the thread.
    """

    def __init__(
        self,
        registry: MetricsRegistry,
        host: str = "127.0.0.1",
        port: int = 9100,
    ) -> None:
        if not isinstance(registry, MetricsRegistry):
            raise ConfigurationError(
                "registry must be a MetricsRegistry, got "
                f"{type(registry).__name__}",
                parameter="registry",
                value=type(registry).__name__,
            )
        if isinstance(port, bool) or not isinstance(port, int):
            raise ConfigurationError(
                f"port must be an integer, got {type(port).__name__}",
                parameter="port",
                value=port,
            )
        if not 0 <= port <= 65535:
            raise ConfigurationError(
                f"port must lie in [0, 65535], got {port}",
                parameter="port",
                value=port,
            )

        self._registry = registry
        handler = type("_BoundMetricsHandler", (_MetricsHandler,), {"registry": registry})
        try:
            self._server = ThreadingHTTPServer((host, port), handler)
        except OSError as error:
            raise ConfigurationError(
                f"Could not bind the metrics server to {host}:{port}: {error}",
                parameter="port",
                value=port,
            ) from error
        self._thread: Optional[threading.Thread] = None
        self._closed = False

    @property
    def port(self) -> int:
        """Port actually bound, which differs from the request when 0 was given."""
        return self._server.server_address[1]

    @property
    def host(self) -> str:
        """Address actually bound."""
        return self._server.server_address[0]

    @property
    def url(self) -> str:
        """Full URL a scraper should poll."""
        return f"http://{self.host}:{self.port}{METRICS_PATH}"

    @property
    def is_running(self) -> bool:
        """Whether the serving thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> "MetricsServer":
        """Begin serving on a background thread."""
        if self.is_running:
            return self
        if self._closed:
            raise ConfigurationError(
                "This metrics server has been stopped and its socket closed; "
                "construct a new one rather than restarting it",
                parameter="server",
                value=None,
            )
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="reliadl-metrics",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """
        Stop serving and release the port. Safe to call more than once.

        ``shutdown`` may only be called while ``serve_forever`` is running: it
        waits on an event that only the serving loop sets, so calling it on a
        server that was never started — or already stopped — blocks forever.
        A stop that hangs is worse than one that fails, because a daemon thread
        gives no indication of where the process is stuck, so both are guarded
        rather than assumed.
        """
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=timeout)
            self._thread = None
        if not self._closed:
            self._server.server_close()
            self._closed = True

    def __enter__(self) -> "MetricsServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def __repr__(self) -> str:
        return f"MetricsServer(url={self.url!r}, running={self.is_running})"
