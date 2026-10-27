"""Clock uncertainty and commit attribution must survive report generation."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import latency_host
import report_latency as report


class LatencyReportTests(unittest.TestCase):
    def device(self):
        return {"format_version": 1, "local": [
            {"sample_id": 0, "queued_us": 1_000, "pending_saved_us": 11_000,
             "framebuffer_us": 16_000, "panel_submitted_us": 21_000,
             "rpc_done_us": 101_000, "sqlite_persisted_us": 201_000, "outcome": "accepted"},
            {"sample_id": 1, "queued_us": 500_000, "framebuffer_us": 530_000,
             "panel_submitted_us": 540_000, "rpc_done_us": 600_000, "outcome": "rejected"}],
            "downloads": [{"sample_id": 2, "armed_us": 1_500,
                           "sqlite_persisted_us": 3_000, "framebuffer_us": 3_100,
                           "panel_submitted_us": 3_200}], "dropped": 1}

    def host(self):
        return {"format_version": 1,
                "sync_probes": [{"host_send_us": 10_000, "device_us": 1_000, "host_receive_us": 10_020},
                                {"host_send_us": 13_000, "device_us": 4_000, "host_receive_us": 13_020}],
                "download_rpc": [{"sample_id": 2, "host_send_us": 11_000,
                                  "host_receive_us": 11_200, "accepted": True, "duplicate": False}]}

    def test_cross_clock_bounds_retain_rpc_and_serial_uncertainty(self):
        result = report.summarize(self.device(), self.host(), 0)
        metric = result["downloads"]["download_sqlite"]
        self.assertEqual(metric["samples"], [{"sample_id": 2, "lower_ms": .8, "upper_ms": 1.02}])
        self.assertEqual(metric["lower_bounds"]["p95_ms"], .8)
        self.assertEqual(result["downloads"]["download_panel_submission"]["samples"][0]["upper_ms"], 1.22)
        self.assertEqual(result["excluded"], [])
        self.assertFalse(result["physical_pixels"]["available"])
        self.assertEqual(result["dropped_device_samples"], 1)

    def test_local_metrics_use_write_origin_and_exclude_rejected_uploads(self):
        result = report.summarize(self.device())
        self.assertEqual(result["local"]["local_framebuffer"], {
            "description": "Local write → framebuffer draw complete",
            "count": 2, "min_ms": 15.0, "p50_ms": 15.0, "p95_ms": 30.0, "max_ms": 30.0})
        self.assertEqual(result["local"]["local_upload_ack_upper_bound"]["count"], 1)
        self.assertEqual(result["local"]["local_upload_ack_upper_bound"]["p50_ms"], 100)
        self.assertEqual(result["local"]["local_sqlite_roundtrip"]["p50_ms"], 200)
        self.assertEqual(result["sample_counts"]["local_outcomes"]["rejected"], 1)
        self.assertFalse(result["local_sqlite_direct"]["available"])

    def test_download_without_explicit_drift_assumption_is_unavailable(self):
        result = report.summarize(self.device(), self.host())
        self.assertEqual(result["downloads"]["download_sqlite"]["count"], 0)
        self.assertIn("clock_drift_ppm", result["excluded"][0]["reason"])

    def test_drift_budget_expands_mapping_with_elapsed_time(self):
        probe = [{"host_send_us": 10_000, "device_us": 1_000, "host_receive_us": 10_020}]
        self.assertEqual(report.host_interval(1_001_000, probe, 100), (1_009_900, 1_010_120))

    def test_response_after_device_publication_gives_zero_lower_bound(self):
        host = self.host()
        host["download_rpc"][0]["host_receive_us"] = 13_000
        result = report.summarize(self.device(), host, 0)
        self.assertEqual(result["downloads"]["download_sqlite"]["samples"][0]["lower_ms"], 0)

    def test_duplicate_rpc_cannot_supply_original_commit_window(self):
        host = self.host()
        host["download_rpc"][0]["duplicate"] = True
        result = report.summarize(self.device(), host, 0)
        self.assertEqual(result["downloads"]["download_sqlite"]["count"], 0)
        self.assertIn("duplicate", result["excluded"][0]["reason"])

    def test_inconsistent_clocks_and_causality_are_excluded(self):
        host = self.host()
        host["sync_probes"][1]["host_send_us"] += 100
        host["sync_probes"][1]["host_receive_us"] += 100
        result = report.summarize(self.device(), host, 0)
        self.assertIn("inconsistent", result["excluded"][0]["reason"])
        host = self.host()
        host["download_rpc"][0]["host_send_us"] = 13_000
        host["download_rpc"][0]["host_receive_us"] = 13_010
        result = report.summarize(self.device(), host, 0)
        self.assertIn("precedes", result["excluded"][0]["reason"])

    def test_duplicate_sample_ids_and_backwards_local_time_fail_closed(self):
        device = self.device()
        device["local"].append(device["local"][0])
        with self.assertRaisesRegex(ValueError, "duplicate local"):
            report.summarize(device)
        device = self.device()
        device["local"][0]["framebuffer_us"] = 0
        with self.assertRaisesRegex(ValueError, "precedes queued"):
            report.summarize(device)

    def test_empty_capture_does_not_claim_zero_latency(self):
        result = report.summarize({"format_version": 1})
        self.assertEqual(result["local"]["local_framebuffer"]["count"], 0)
        self.assertNotIn("p50_ms", result["local"]["local_framebuffer"])


class HostHelperTests(unittest.TestCase):
    def session(self):
        return {"url": "https://example.supabase.co", "key": "private-key", "access_token": "private-token",
                "room_id": "550e8400-e29b-41d4-a716-446655440000"}

    def move(self):
        return {"id": "550e8400-e29b-41d4-a716-446655440001", "orientation": "h", "row": 0, "col": 0}

    def test_clock_probe_brackets_device_read_and_checks_request_identity(self):
        values = iter((10_000, 10_200))
        def command(message):
            return {**message, "device_us": 1_000}
        self.assertEqual(latency_host.clock_probe(command, 8, lambda: next(values)), {
            "host_send_us": 10_000, "device_us": 1_000, "host_receive_us": 10_200})
        with self.assertRaisesRegex(ValueError, "Mismatched"):
            latency_host.clock_probe(lambda _: {"command": "clock", "request_id": 7, "device_us": 1_000}, 8)

    def test_single_rpc_is_bracketed_and_no_credentials_exported(self):
        calls = []
        def transport(request, timeout):
            calls.append(request)
            self.assertEqual(request.full_url, "https://example.supabase.co/rest/v1/rpc/submit_move")
            self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
            return 200, {"ok": True, "move_index": 3}
        values = iter((10_000, 10_200))
        row = latency_host.submit_move(self.session(), self.move(), 2, lambda: next(values), transport)
        self.assertEqual(row, {"sample_id": 2, "host_send_us": 10_000, "host_receive_us": 10_200,
                               "accepted": True, "duplicate": False, "http_status": 200, "move_index": 3})
        self.assertEqual(len(calls), 1)
        self.assertNotIn("private", repr(row))
        self.assertNotIn(self.move()["id"], repr(row))

    def test_rpc_duplicate_marked_and_errors_are_not_retried(self):
        values = iter((1, 2))
        row = latency_host.submit_move(self.session(), self.move(), 2, lambda: next(values),
                                       lambda *_: (200, {"ok": True, "duplicate": True}))
        self.assertTrue(row["duplicate"])
        calls = []
        def fail(*_args):
            calls.append(1)
            raise OSError("network outcome unknown")
        with self.assertRaises(OSError):
            latency_host.submit_move(self.session(), self.move(), 2, transport=fail)
        self.assertEqual(len(calls), 1)

    def test_arming_failure_prevents_a_claim_of_correlation(self):
        with self.assertRaisesRegex(ValueError, "did not arm"):
            latency_host.arm_download(lambda message: {**message, "accepted": False}, 2, self.move()["id"], ["h", 0, 0], 3, 8)

    def test_bad_origin_and_outside_board_fail_before_network(self):
        session = {**self.session(), "url": "http://example.supabase.co"}
        with self.assertRaisesRegex(ValueError, "HTTPS origin"):
            latency_host.submit_move(session, self.move(), 2)
        with self.assertRaisesRegex(ValueError, "outside"):
            latency_host.submit_move(self.session(), {**self.move(), "col": 5}, 2)


if __name__ == "__main__":
    unittest.main()
