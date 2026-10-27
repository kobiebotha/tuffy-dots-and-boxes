"""Pooling must preserve each capture's clocks and explicit warmup decisions."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from summarize_latency_runs import summarize_runs


def capture(offset=9_000, local_duration=20_000, remote_duration=1_000):
    return {"format_version": 1, "clock_drift_ppm": 0,
            "device": {"format_version": 1, "dropped": 0,
                "harness": {"source_sha256": "same-probe"},
                "local": [{"sample_id": 0, "queued_us": 100_000,
                           "framebuffer_us": 100_000 + local_duration - 5_000,
                           "panel_submitted_us": 100_000 + local_duration,
                           "rpc_done_us": 300_000, "outcome": "accepted"}],
                "downloads": [{"sample_id": 0, "armed_us": 1_000,
                               "sqlite_persisted_us": 2_000 + remote_duration,
                               "framebuffer_us": 2_100 + remote_duration,
                               "panel_submitted_us": 2_200 + remote_duration}]},
            "host": {"format_version": 1,
                "sync_probes": [{"host_send_us": 1_000 + offset, "device_us": 1_000,
                                 "host_receive_us": 1_020 + offset}],
                "download_rpc": [{"sample_id": 0, "host_send_us": 2_000 + offset,
                                  "host_receive_us": 2_200 + offset,
                                  "accepted": True, "duplicate": False}]},
            "capture": {"status": "complete", "annotations": []}}


class LatencySummaryTests(unittest.TestCase):
    def test_clock_resets_are_mapped_per_run_before_pooling(self):
        first = capture(offset=9_000, local_duration=20_000, remote_duration=1_000)
        second = capture(offset=9_000_000, local_duration=40_000, remote_duration=2_000)
        result = summarize_runs([first, second], ["first", "second"])
        local = result["local"]["local_panel_submission"]
        self.assertEqual(local["count"], 2)
        self.assertEqual(local["p50_ms"], 20)
        self.assertEqual(local["p95_ms"], 40)
        remote = result["downloads"]["download_sqlite"]
        self.assertEqual(remote["samples"], [
            {"run_id": "first", "sample_id": 0, "lower_ms": .8, "upper_ms": 1.02},
            {"run_id": "second", "sample_id": 0, "lower_ms": 1.8, "upper_ms": 2.02}])
        self.assertEqual(remote["lower_bounds"]["p50_ms"], .8)
        self.assertEqual(remote["upper_bounds"]["p95_ms"], 2.02)
        self.assertEqual(result["totals"]["complete_runs"], 2)

    def test_warmup_annotations_join_by_kind_and_sample_id(self):
        source = capture()
        source["device"]["local"].append({"sample_id": 1, "queued_us": 500_000,
                                          "panel_submitted_us": 550_000, "outcome": "accepted"})
        source["capture"]["annotations"] = [
            {"kind": "local", "sample_id": 0, "move_index": 1, "warmup": True},
            {"kind": "local", "sample_id": 1, "move_index": 3, "warmup": False},
            {"kind": "download", "sample_id": 0, "move_index": 2, "warmup": False}]
        result = summarize_runs([source])
        self.assertEqual(result["totals"]["warmup_local"], 1)
        self.assertEqual(result["totals"]["warmup_download"], 0)
        self.assertEqual(result["local"]["local_panel_submission"]["samples"], [
            {"run_id": "run1", "sample_id": 1, "move_index": 3, "milliseconds": 50.0}])
        self.assertEqual(result["downloads"]["download_sqlite"]["samples"][0]["move_index"], 2)
        # Identical numeric IDs in opposite directions are independent.
        self.assertEqual(result["downloads"]["download_sqlite"]["count"], 1)

    def test_cli_and_annotation_warmups_form_union_without_double_removal(self):
        source = capture()
        source["capture"]["annotations"] = [{"kind": "local", "sample_id": 0, "warmup": True}]
        result = summarize_runs([source], warmup_local=1, warmup_download=1)
        self.assertEqual(result["totals"]["warmup_local"], 1)
        self.assertEqual(result["totals"]["retained_local"], 0)
        self.assertEqual(result["local"]["local_panel_submission"]["count"], 0)
        self.assertNotIn("p50_ms", result["local"]["local_panel_submission"])
        self.assertEqual(len(result["runs"][0]["warmup_exclusions"][0]["reasons"]), 2)

    def test_bad_clock_run_does_not_poison_independent_valid_run(self):
        valid, invalid = capture(), capture()
        invalid["host"]["sync_probes"].append({"host_send_us": 20_000,
            "device_us": 1_000, "host_receive_us": 20_020})
        result = summarize_runs([valid, invalid])
        self.assertEqual(result["downloads"]["download_sqlite"]["count"], 1)
        self.assertIn("inconsistent", result["runs"][1]["report"]["excluded"][0]["reason"])
        self.assertEqual(result["local"]["local_panel_submission"]["count"], 2)

    def test_failure_duplicate_drop_and_incomplete_counts_remain_visible(self):
        source = capture()
        source["capture"]["status"] = "failed"
        source["device"]["dropped"] = 2
        source["device"]["local"][0]["outcome"] = "rejected"
        source["host"]["download_rpc"][0]["duplicate"] = True
        result = summarize_runs([source])
        self.assertEqual(result["totals"]["complete_runs"], 0)
        self.assertEqual(result["totals"]["dropped_device_samples"], 2)
        self.assertEqual(result["totals"]["host_rpc_duplicates"], 1)
        self.assertEqual(result["local"]["local_upload_ack_upper_bound"]["count"], 0)
        self.assertEqual(result["local"]["local_panel_submission"]["count"], 1)
        self.assertEqual(result["downloads"]["download_sqlite"]["count"], 0)
        self.assertEqual(result["runs"][0]["report"]["sample_counts"]["local_outcomes"]["rejected"], 1)

    def test_explicit_drift_override_is_recorded_as_assumption(self):
        result = summarize_runs([capture()], clock_drift_ppm=1000)
        self.assertEqual(result["clock_drift_ppm_assumptions"], [1000])
        self.assertEqual(result["runs"][0]["clock_drift_ppm_assumption"], 1000)
        self.assertTrue(any("engineering assumption" in text for text in result["limits"]))

    def test_duplicate_annotations_and_corrupt_warmup_rows_fail_closed(self):
        source = capture()
        source["capture"]["annotations"] = [{"kind": "local", "sample_id": 0, "warmup": True}] * 2
        with self.assertRaisesRegex(ValueError, "Duplicate sample annotation"):
            summarize_runs([source])
        source = capture()
        source["device"]["local"].append(source["device"]["local"][0])
        with self.assertRaisesRegex(ValueError, "duplicate local"):
            summarize_runs([source], warmup_local=2)

    def test_different_probe_sources_are_disclosed(self):
        first, second = capture(), capture()
        second["device"]["harness"]["source_sha256"] = "changed-probe"
        result = summarize_runs([first, second])
        self.assertEqual(result["instrumentation_source_sha256s"], ["changed-probe", "same-probe"])
        self.assertTrue(any("different instrumentation" in text for text in result["limits"]))


if __name__ == "__main__":
    unittest.main()
