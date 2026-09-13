"""
Unit tests for the Prometheus metrics subsystem in src.telemetry.metrics.

Covers the three metric types and the exposition grammar they render into —
cumulative histogram buckets, counter monotonicity, and the escaping rules that
decide whether a scraper accepts the payload at all — plus the background
server and the per-observation cost the transfer path pays.
"""

from __future__ import annotations

import math
import threading
import timeit
import unittest
import urllib.error
import urllib.request
from typing import List

from src.exceptions import ConfigurationError
from src.telemetry.metrics import (
    CONTENT_TYPE,
    DEFAULT_DURATION_BUCKETS,
    DEFAULT_THROUGHPUT_BUCKETS,
    Counter,
    DownloadMetrics,
    Gauge,
    Histogram,
    MetricsRegistry,
    MetricsServer,
    escape_help,
    escape_label_value,
    format_value,
)

MB = 1024 * 1024


class TestValueFormatting(unittest.TestCase):
    """Numbers must be spelled the way the exposition format expects."""

    def test_integral_values_lose_the_trailing_zero(self) -> None:
        """Counters are most samples; keeping them short keeps payloads small."""
        self.assertEqual(format_value(5.0), "5")
        self.assertEqual(format_value(0.0), "0")
        self.assertEqual(format_value(-3.0), "-3")

    def test_fractional_values_are_preserved(self) -> None:
        self.assertEqual(format_value(0.25), "0.25")
        self.assertIn("1.5", format_value(1.5))

    def test_infinities_use_the_prometheus_spelling(self) -> None:
        """Python's 'inf' is not what a scraper parses."""
        self.assertEqual(format_value(math.inf), "+Inf")
        self.assertEqual(format_value(-math.inf), "-Inf")

    def test_nan(self) -> None:
        self.assertEqual(format_value(math.nan), "NaN")


class TestEscaping(unittest.TestCase):
    """
    Help text and label values escape by different rules.

    An unescaped newline terminates the line early; an unescaped quote in a
    label closes the value and corrupts every sample after it on that line.
    """

    def test_help_escapes_backslash_and_newline_only(self) -> None:
        self.assertEqual(escape_help("a\\b"), "a\\\\b")
        self.assertEqual(escape_help("a\nb"), "a\\nb")
        self.assertEqual(escape_help('a"b'), 'a"b')

    def test_label_value_also_escapes_quotes(self) -> None:
        self.assertEqual(escape_label_value('a"b'), 'a\\"b')
        self.assertEqual(escape_label_value("a\\b"), "a\\\\b")
        self.assertEqual(escape_label_value("a\nb"), "a\\nb")

    def test_a_quoted_label_value_survives_rendering(self) -> None:
        counter = Counter("m", "d", ["mirror"])
        counter.inc(1, mirror='cdn "east"')
        line = counter.collect()[0].render()
        self.assertEqual(line, 'm{mirror="cdn \\"east\\""} 1')
        # Exactly two unescaped quotes delimit the value.
        self.assertEqual(line.count('"') - line.count('\\"'), 2)


class TestCounter(unittest.TestCase):
    """A counter only ever goes up."""

    def test_increments_accumulate(self) -> None:
        counter = Counter("c", "d")
        counter.inc()
        counter.inc(5)
        self.assertEqual(counter.value(), 6)

    def test_decrement_is_refused(self) -> None:
        """
        Prometheus reads a falling counter as a process restart.

        rate() then turns the gap into an enormous spurious spike rather than
        the obviously-wrong negative rate that would at least be noticed.
        """
        counter = Counter("c", "d")
        with self.assertRaises(ConfigurationError) as caught:
            counter.inc(-1)
        self.assertIn("cannot decrease", str(caught.exception))

    def test_non_finite_increment_refused(self) -> None:
        counter = Counter("c", "d")
        for bad in (math.inf, math.nan):
            with self.assertRaises(ConfigurationError):
                counter.inc(bad)

    def test_non_numeric_increment_refused(self) -> None:
        counter = Counter("c", "d")
        for bad in ("1", None, True):
            with self.assertRaises(ConfigurationError):
                counter.inc(bad)  # type: ignore[arg-type]

    def test_render_includes_help_and_type(self) -> None:
        counter = Counter("c", "A described counter.")
        counter.inc()
        rendered = counter.render()
        self.assertIn("# HELP c A described counter.", rendered)
        self.assertIn("# TYPE c counter", rendered)
        self.assertIn("c 1", rendered)


class TestGauge(unittest.TestCase):
    """A gauge may move in either direction."""

    def test_set_inc_dec(self) -> None:
        gauge = Gauge("g", "d")
        gauge.set(10)
        gauge.inc(5)
        gauge.dec(3)
        self.assertEqual(gauge.value(), 12)

    def test_negative_values_are_allowed(self) -> None:
        """The whole point of a gauge over a counter."""
        gauge = Gauge("g", "d")
        gauge.set(-5)
        self.assertEqual(gauge.value(), -5)

    def test_non_finite_refused(self) -> None:
        gauge = Gauge("g", "d")
        for bad in (math.inf, math.nan):
            with self.assertRaises(ConfigurationError):
                gauge.set(bad)

    def test_type_line(self) -> None:
        gauge = Gauge("g", "d")
        gauge.set(1)
        self.assertIn("# TYPE g gauge", gauge.render())


class TestHistogramBuckets(unittest.TestCase):
    """
    Buckets are cumulative, and `le` means 'at or below'.

    Emitting disjoint counts produces a histogram that parses cleanly and yields
    nonsense quantiles, which is worse than failing outright.
    """

    def test_buckets_are_cumulative(self) -> None:
        histogram = Histogram("h", "d", buckets=(1.0, 2.0, 3.0))
        for value in (0.5, 1.5, 2.5, 3.5):
            histogram.observe(value)
        self.assertEqual(histogram.bucket_counts(), [1, 2, 3, 4])

    def test_a_value_on_a_bound_falls_inside_it(self) -> None:
        """
        `le="0.5"` must include an observation of exactly 0.5.

        bisect_right would push it into the next bucket, and the two agree
        everywhere except on the bounds themselves — precisely where a
        histogram's edges get checked.
        """
        histogram = Histogram("h", "d", buckets=(0.1, 0.5, 1.0))
        histogram.observe(0.5)
        self.assertEqual(histogram.bucket_counts(), [0, 1, 1, 1])

    def test_every_bound_is_checked_for_inclusion(self) -> None:
        bounds = (0.1, 0.5, 1.0, 5.0)
        for index, bound in enumerate(bounds):
            histogram = Histogram("h", "d", buckets=bounds)
            histogram.observe(bound)
            counts = histogram.bucket_counts()
            self.assertEqual(counts[index], 1, f"{bound} missing from le={bound}")
            if index:
                self.assertEqual(counts[index - 1], 0)

    def test_inf_bucket_equals_the_observation_count(self) -> None:
        """Mandatory, and always equal to the count."""
        histogram = Histogram("h", "d", buckets=(1.0, 2.0))
        for value in (0.5, 1.5, 100.0):
            histogram.observe(value)
        self.assertEqual(histogram.bucket_counts()[-1], histogram.sample_count())
        self.assertEqual(histogram.sample_count(), 3)

    def test_sum_and_count(self) -> None:
        histogram = Histogram("h", "d", buckets=(1.0, 2.0))
        for value in (0.5, 1.5, 2.5):
            histogram.observe(value)
        self.assertAlmostEqual(histogram.sample_sum(), 4.5)
        self.assertEqual(histogram.sample_count(), 3)

    def test_values_above_every_bound_land_in_inf_only(self) -> None:
        histogram = Histogram("h", "d", buckets=(1.0, 2.0))
        histogram.observe(1000.0)
        self.assertEqual(histogram.bucket_counts(), [0, 0, 1])

    def test_rendered_output_carries_le_labels_and_inf(self) -> None:
        histogram = Histogram("h", "d", buckets=(1.0, 2.0))
        histogram.observe(1.5)
        rendered = histogram.render()
        self.assertIn("# TYPE h histogram", rendered)
        self.assertIn('h_bucket{le="1"} 0', rendered)
        self.assertIn('h_bucket{le="2"} 1', rendered)
        self.assertIn('h_bucket{le="+Inf"} 1', rendered)
        self.assertIn("h_sum 1.5", rendered)
        self.assertIn("h_count 1", rendered)

    def test_unsorted_bounds_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            Histogram("h", "d", buckets=(1.0, 0.5, 2.0))

    def test_duplicate_bounds_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            Histogram("h", "d", buckets=(1.0, 1.0))

    def test_empty_bounds_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            Histogram("h", "d", buckets=())

    def test_infinite_bound_is_dropped_not_rejected(self) -> None:
        """The +Inf bucket is implicit, so an explicit one is redundant."""
        histogram = Histogram("h", "d", buckets=(1.0, 2.0, math.inf))
        self.assertEqual(histogram.bounds, (1.0, 2.0))

    def test_non_finite_observation_refused(self) -> None:
        histogram = Histogram("h", "d", buckets=(1.0,))
        for bad in (math.inf, math.nan):
            with self.assertRaises(ConfigurationError):
                histogram.observe(bad)

    def test_default_bucket_scales_differ_by_orders_of_magnitude(self) -> None:
        """Durations and throughputs cannot share a scale."""
        self.assertLess(max(DEFAULT_DURATION_BUCKETS), 100)
        self.assertGreater(max(DEFAULT_THROUGHPUT_BUCKETS), 1e9)


class TestLabels(unittest.TestCase):
    """Label sets identify series and must be matched exactly."""

    def test_series_are_separated_by_label_value(self) -> None:
        counter = Counter("c", "d", ["mirror"])
        counter.inc(1, mirror="a")
        counter.inc(2, mirror="b")
        self.assertEqual(counter.value(mirror="a"), 1)
        self.assertEqual(counter.value(mirror="b"), 2)
        self.assertEqual(len(counter.collect()), 2)

    def test_keyword_order_does_not_split_a_series(self) -> None:
        """
        Ordering follows the declared names, not the caller's keywords.

        Otherwise the same series would be recorded twice under two keys.
        """
        counter = Counter("c", "d", ["mirror", "region"])
        counter.inc(1, mirror="a", region="eu")
        counter.inc(1, region="eu", mirror="a")
        self.assertEqual(counter.value(mirror="a", region="eu"), 2)
        self.assertEqual(len(counter.collect()), 1)

    def test_partial_label_set_refused(self) -> None:
        """
        A missing label would silently create a different series.

        The intended one then stays flat, which looks like the code path is
        never taken.
        """
        counter = Counter("c", "d", ["mirror", "region"])
        with self.assertRaises(ConfigurationError):
            counter.inc(1, mirror="a")

    def test_unexpected_label_refused(self) -> None:
        counter = Counter("c", "d", ["mirror"])
        with self.assertRaises(ConfigurationError):
            counter.inc(1, mirror="a", extra="x")

    def test_invalid_names_refused(self) -> None:
        for bad in ("has-dash", "1leading", "has space", ""):
            with self.assertRaises(ConfigurationError):
                Counter(bad, "d")
        for bad_label in ("has-dash", "1leading", ""):
            with self.assertRaises(ConfigurationError):
                Counter("c", "d", [bad_label])

    def test_duplicate_label_names_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            Counter("c", "d", ["mirror", "mirror"])


class TestRegistry(unittest.TestCase):
    """The registry is what a scrape renders."""

    def test_duplicate_name_refused(self) -> None:
        """
        Two metrics sharing a name emit two HELP lines.

        A scraper rejects the whole payload rather than the offending metric.
        """
        registry = MetricsRegistry()
        registry.register(Counter("c", "d"))
        with self.assertRaises(ConfigurationError):
            registry.register(Counter("c", "other"))

    def test_non_metric_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            MetricsRegistry().register("not-a-metric")  # type: ignore[arg-type]

    def test_render_ends_with_a_newline(self) -> None:
        """An unterminated final sample is treated as a truncated payload."""
        registry = MetricsRegistry()
        counter = registry.register(Counter("c", "d"))
        counter.inc()
        self.assertTrue(registry.render().endswith("\n"))

    def test_unregister_and_lookup(self) -> None:
        registry = MetricsRegistry()
        registry.register(Counter("c", "d"))
        self.assertIsNotNone(registry.get("c"))
        self.assertEqual(registry.names, ("c",))
        registry.unregister("c")
        self.assertIsNone(registry.get("c"))
        self.assertEqual(registry.names, ())

    def test_empty_registry_renders_cleanly(self) -> None:
        self.assertEqual(MetricsRegistry().render(), "\n")


class TestDownloadMetrics(unittest.TestCase):
    """The catalog the issue specifies, and the facade over it."""

    def setUp(self) -> None:
        self.metrics = DownloadMetrics()

    def test_all_six_metrics_are_registered_with_the_specified_names(self) -> None:
        self.assertEqual(
            set(self.metrics.registry.names),
            {
                "chunkguard_bytes_downloaded_total",
                "chunkguard_chunk_retries_total",
                "chunkguard_chunk_download_duration_seconds",
                "chunkguard_chunk_throughput_bytes_per_second",
                "chunkguard_active_workers",
                "chunkguard_download_progress_ratio",
            },
        )

    def test_metric_types_are_as_specified(self) -> None:
        registry = self.metrics.registry
        self.assertEqual(registry.get("chunkguard_bytes_downloaded_total").metric_type, "counter")
        self.assertEqual(registry.get("chunkguard_chunk_retries_total").metric_type, "counter")
        self.assertEqual(
            registry.get("chunkguard_chunk_download_duration_seconds").metric_type,
            "histogram",
        )
        self.assertEqual(
            registry.get("chunkguard_chunk_throughput_bytes_per_second").metric_type,
            "histogram",
        )
        self.assertEqual(registry.get("chunkguard_active_workers").metric_type, "gauge")
        self.assertEqual(
            registry.get("chunkguard_download_progress_ratio").metric_type, "gauge"
        )

    def test_record_chunk_updates_every_affected_metric(self) -> None:
        self.metrics.record_chunk(8 * MB, 2.0)
        self.assertEqual(self.metrics.bytes_downloaded.value(), 8 * MB)
        self.assertEqual(self.metrics.chunk_duration.sample_count(), 1)
        self.assertEqual(self.metrics.chunk_throughput.sample_count(), 1)
        self.assertAlmostEqual(self.metrics.chunk_throughput.sample_sum(), 4 * MB)

    def test_throughput_is_derived_not_supplied(self) -> None:
        """The two histograms cannot disagree about the same transfer."""
        self.metrics.record_chunk(10 * MB, 5.0)
        self.assertAlmostEqual(self.metrics.chunk_throughput.sample_sum(), 2 * MB)

    def test_zero_duration_records_bytes_but_no_throughput(self) -> None:
        """
        Dividing by zero would report an infinite rate.

        It would land in the top bucket and stay there, quietly skewing every
        quantile drawn from the histogram.
        """
        self.metrics.record_chunk(8 * MB, 0.0)
        self.assertEqual(self.metrics.bytes_downloaded.value(), 8 * MB)
        self.assertEqual(self.metrics.chunk_throughput.sample_count(), 0)
        self.assertEqual(self.metrics.chunk_duration.sample_count(), 0)

    def test_progress_is_a_clamped_ratio(self) -> None:
        self.metrics.set_progress(25, 100)
        self.assertAlmostEqual(self.metrics.progress_ratio.value(), 0.25)
        self.metrics.set_progress(200, 100)
        self.assertEqual(self.metrics.progress_ratio.value(), 1.0)

    def test_unknown_total_reports_zero_not_nan(self) -> None:
        """NaN in a gauge breaks arithmetic in every dashboard that touches it."""
        self.metrics.set_progress(10, 0)
        self.assertEqual(self.metrics.progress_ratio.value(), 0.0)

    def test_retries_and_workers(self) -> None:
        self.metrics.record_retry()
        self.metrics.record_retry(3)
        self.assertEqual(self.metrics.chunk_retries.value(), 4)
        self.metrics.set_active_workers(8)
        self.assertEqual(self.metrics.active_workers.value(), 8)

    def test_labels_flow_through_the_facade(self) -> None:
        metrics = DownloadMetrics(namespace="chunkguard", label_names=["mirror"])
        metrics.record_chunk(1024, 1.0, mirror="cdn-a")
        metrics.record_chunk(2048, 1.0, mirror="cdn-b")
        self.assertEqual(metrics.bytes_downloaded.value(mirror="cdn-a"), 1024)
        self.assertEqual(metrics.bytes_downloaded.value(mirror="cdn-b"), 2048)

    def test_custom_namespace(self) -> None:
        metrics = DownloadMetrics(namespace="reliadl")
        self.assertIn("reliadl_bytes_downloaded_total", metrics.registry.names)

    def test_rendered_payload_is_well_formed(self) -> None:
        self.metrics.record_chunk(8 * MB, 1.0)
        self.metrics.set_active_workers(4)
        rendered = self.metrics.render()
        # Every metric declares HELP and TYPE exactly once.
        for name in self.metrics.registry.names:
            self.assertEqual(rendered.count(f"# HELP {name} "), 1, name)
            self.assertEqual(rendered.count(f"# TYPE {name} "), 1, name)
        for line in rendered.splitlines():
            self.assertTrue(line.startswith("#") or " " in line, line)


class TestMetricsServer(unittest.TestCase):
    """The background exposition endpoint."""

    def setUp(self) -> None:
        self.metrics = DownloadMetrics()
        self.metrics.record_chunk(8 * MB, 1.0)
        self.server = MetricsServer(self.metrics.registry, port=0).start()
        self.addCleanup(self.server.stop)

    def test_serves_the_registry(self) -> None:
        with urllib.request.urlopen(self.server.url, timeout=5) as response:
            body = response.read().decode("utf-8")
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Content-Type"], CONTENT_TYPE)
        self.assertIn("chunkguard_bytes_downloaded_total 8388608", body)

    def test_reflects_later_updates(self) -> None:
        """The payload is rendered per scrape, not snapshotted at startup."""
        self.metrics.record_chunk(8 * MB, 1.0)
        with urllib.request.urlopen(self.server.url, timeout=5) as response:
            self.assertIn(
                "chunkguard_bytes_downloaded_total 16777216",
                response.read().decode("utf-8"),
            )

    def test_other_paths_are_not_found(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(
                f"http://{self.server.host}:{self.server.port}/", timeout=5
            )
        self.assertEqual(caught.exception.code, 404)

    def test_query_string_is_ignored(self) -> None:
        url = f"{self.server.url}?collect[]=x"
        with urllib.request.urlopen(url, timeout=5) as response:
            self.assertEqual(response.status, 200)

    def test_ephemeral_port_is_reported(self) -> None:
        self.assertGreater(self.server.port, 0)
        self.assertIn(str(self.server.port), self.server.url)

    def test_is_running_tracks_the_thread(self) -> None:
        self.assertTrue(self.server.is_running)
        self.server.stop()
        self.assertFalse(self.server.is_running)

    def test_start_is_idempotent(self) -> None:
        self.server.start()
        self.assertTrue(self.server.is_running)

    def test_concurrent_scrapes_are_served(self) -> None:
        """A scraper plus a human with a browser is the normal case."""
        results: List[int] = []
        guard = threading.Lock()

        def scrape() -> None:
            with urllib.request.urlopen(self.server.url, timeout=10) as response:
                code = response.status
                response.read()
            with guard:
                results.append(code)

        threads = [threading.Thread(target=scrape) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(results, [200] * 8)

    def test_context_manager_stops_the_server(self) -> None:
        registry = MetricsRegistry()
        with MetricsServer(registry, port=0) as server:
            self.assertTrue(server.is_running)
            port = server.port
        self.assertFalse(server.is_running)

        # The listening socket is closed, not merely unreferenced: binding the
        # same port again is the evidence. A leaked listener would fail here.
        rebound = MetricsServer(registry, port=port)
        self.assertEqual(rebound.port, port)
        rebound.stop()

    def test_stop_is_idempotent_and_safe_before_start(self) -> None:
        """
        Regression guard: stopping twice used to hang the process.

        BaseServer.shutdown() waits on an event only the serving loop sets, so
        calling it on a server that was never started — or already stopped —
        blocks forever. A hang is a worse failure than an exception, because a
        daemon thread leaves no indication of where the process is stuck, which
        is exactly how this surfaced.
        """
        never_started = MetricsServer(MetricsRegistry(), port=0)
        never_started.stop()

        started = MetricsServer(MetricsRegistry(), port=0).start()
        started.stop()
        started.stop()
        self.assertFalse(started.is_running)

    def test_a_stopped_server_refuses_to_restart(self) -> None:
        """Its socket is closed; a restart would serve on a dead listener."""
        server = MetricsServer(MetricsRegistry(), port=0).start()
        server.stop()
        with self.assertRaises(ConfigurationError):
            server.start()

    def test_invalid_construction_refused(self) -> None:
        with self.assertRaises(ConfigurationError):
            MetricsServer("not-a-registry")  # type: ignore[arg-type]
        for bad in (-1, 70000):
            with self.assertRaises(ConfigurationError):
                MetricsServer(MetricsRegistry(), port=bad)
        with self.assertRaises(ConfigurationError):
            MetricsServer(MetricsRegistry(), port="9100")  # type: ignore[arg-type]

    def test_port_conflict_is_raised_to_the_caller(self) -> None:
        """
        Binding happens at construction, not inside the thread.

        A conflict discovered on the thread would be invisible: the caller would
        hold a server object that never serves.
        """
        with self.assertRaises(ConfigurationError):
            MetricsServer(MetricsRegistry(), host=self.server.host, port=self.server.port)


class TestObservationOverhead(unittest.TestCase):
    """
    Acceptance criterion: minimal performance overhead.

    An observation sits on the transfer path, once per chunk. The budget is
    expressed against the chunk rate a transfer actually produces rather than
    against a raw call count, since the latter says nothing about the cost the
    download pays.
    """

    def test_record_chunk_cost_is_well_under_one_percent_of_a_core(self) -> None:
        """
        The thresholds are chosen to survive a slow shared CI runner.

        A budget the implementation clears by only a few times would fail on a
        loaded Windows runner without anything having regressed, and a flaky
        assertion about performance teaches nobody anything. Both bounds below
        leave roughly a 50x margin over the measured cost, so a failure means a
        real change in the hot path rather than a busy machine.
        """
        metrics = DownloadMetrics()
        iterations = 20_000
        elapsed = timeit.timeit(
            lambda: metrics.record_chunk(8 * MB, 1.5), number=iterations
        )
        per_call = elapsed / iterations

        # At the 8 MB default chunk size, 1 GB/s is 125 chunks per second and a
        # more typical 100 MB/s is 12.5. The first is asserted against the 1%
        # criterion, the second against a tenth of it.
        for rate_bytes_per_second, ceiling in ((1000 * MB, 0.01), (100 * MB, 0.001)):
            chunks_per_second = rate_bytes_per_second / (8 * MB)
            cpu_fraction = chunks_per_second * per_call
            self.assertLess(
                cpu_fraction,
                ceiling,
                f"{per_call * 1e6:.2f}us per observation costs "
                f"{cpu_fraction * 100:.4f}% of a core at "
                f"{rate_bytes_per_second / MB:.0f} MB/s",
            )

    def test_bucket_search_does_not_scale_with_bucket_count(self) -> None:
        """
        Binary search, not a scan.

        It matters little at thirteen bounds, but the cost should not grow with
        however many an operator decides to configure.
        """
        few = Histogram("h", "d", buckets=tuple(float(i) for i in range(1, 9)))
        many = Histogram("h", "d", buckets=tuple(float(i) for i in range(1, 513)))
        iterations = 20_000
        small = timeit.timeit(lambda: few.observe(1000.0), number=iterations)
        large = timeit.timeit(lambda: many.observe(1000.0), number=iterations)
        # 64x the bounds must not cost anything like 64x the time.
        self.assertLess(large, small * 4)

    def test_rendering_is_cheap_enough_to_scrape_often(self) -> None:
        metrics = DownloadMetrics()
        for _ in range(100):
            metrics.record_chunk(8 * MB, 1.0)
        elapsed = timeit.timeit(metrics.render, number=500) / 500
        self.assertLess(elapsed, 0.05)


class TestThreadSafety(unittest.TestCase):
    """Workers record concurrently while a scraper renders."""

    def test_counter_increments_are_not_lost(self) -> None:
        counter = Counter("c", "d")
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait(timeout=10)
            for _ in range(2000):
                counter.inc()

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(counter.value(), 16000)

    def test_histogram_observations_are_not_lost(self) -> None:
        histogram = Histogram("h", "d", buckets=(1.0, 2.0, 3.0))
        barrier = threading.Barrier(6)

        def worker() -> None:
            barrier.wait(timeout=10)
            for _ in range(1000):
                histogram.observe(1.5)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(histogram.sample_count(), 6000)
        self.assertEqual(histogram.bucket_counts()[-1], 6000)

    def test_rendering_during_updates_stays_well_formed(self) -> None:
        metrics = DownloadMetrics()
        stop = threading.Event()
        errors: List[BaseException] = []

        def record() -> None:
            try:
                while not stop.is_set():
                    metrics.record_chunk(1024, 0.5)
            except BaseException as error:  # noqa: BLE001 - surfaced below
                errors.append(error)

        def scrape() -> None:
            try:
                for _ in range(50):
                    payload = metrics.render()
                    self.assertTrue(payload.endswith("\n"))
            except BaseException as error:  # noqa: BLE001 - surfaced below
                errors.append(error)

        writer = threading.Thread(target=record)
        writer.start()
        reader = threading.Thread(target=scrape)
        reader.start()
        reader.join(timeout=30)
        stop.set()
        writer.join(timeout=30)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
