"""Pool derived latency samples from independent device captures.

Each capture keeps its own monotonic clock, synchronization probes and RPC
brackets. Only durations and latency intervals are pooled. Warmup annotations
are applied by (kind, sample_id) before any metric or percentile is calculated.
"""
import argparse
import hashlib
import json
from pathlib import Path

from report_latency import (DOWNLOAD_METRICS, LOCAL_METRICS, indexed,
                            statistics, summarize, timestamp)


FEEDBACK_METRICS = ("local_durable_pending", "local_framebuffer", "local_panel_submission")


def _warmup_count(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(label + " must be a nonnegative integer")
    return value


def _annotations(capture):
    result = {}
    for row in capture.get("capture", {}).get("annotations", []):
        kind = row.get("kind")
        if kind not in ("local", "download"):
            raise ValueError("Unknown sample annotation kind")
        identity = timestamp(row.get("sample_id"), "annotation sample_id")
        if (kind, identity) in result:
            raise ValueError("Duplicate sample annotation")
        if "warmup" in row and type(row["warmup"]) is not bool:
            raise ValueError("Annotation warmup must be boolean")
        if "move_index" in row:
            timestamp(row["move_index"], "annotation move_index")
        result[kind, identity] = row
    return result


def _filter_rows(rows, kind, count, annotations):
    # Validate before filtering: warmup exclusion must not hide corrupt or
    # duplicated sample identities in a capture.
    indexed(rows, kind)
    kept, removed = [], []
    for position, row in enumerate(rows):
        identity = row["sample_id"]
        annotation = annotations.get((kind, identity), {})
        reasons = []
        if row.get("warmup") is True or annotation.get("warmup") is True:
            reasons.append("warmup annotation")
        if position < count:
            reasons.append("first samples requested by CLI")
        if reasons:
            removed.append({"kind": kind, "sample_id": identity, "reasons": reasons})
        else:
            kept.append(row)
    return kept, removed


def _sample_key(run_id, kind, row, annotations):
    key = {"run_id": run_id, "sample_id": row["sample_id"]}
    annotation = annotations.get((kind, row["sample_id"]), {})
    if "move_index" in annotation:
        key["move_index"] = annotation["move_index"]
    return key


def summarize_runs(captures, names=None, warmup_local=0, warmup_download=0,
                   clock_drift_ppm=None):
    """Return pooled descriptive statistics without joining independent clocks."""
    _warmup_count(warmup_local, "warmup_local")
    _warmup_count(warmup_download, "warmup_download")
    if not captures:
        raise ValueError("At least one capture is required")
    names = names or ["run" + str(index + 1) for index in range(len(captures))]
    if len(names) != len(captures) or len(set(names)) != len(names):
        raise ValueError("Each capture requires a distinct run name")
    local_samples = {metric: [] for metric in LOCAL_METRICS}
    remote_samples = {metric: [] for metric in DOWNLOAD_METRICS}
    result = {"format_version": 1, "runs": [], "local": {}, "downloads": {},
              "warmup_policy": {"local_first_samples_per_run": warmup_local,
                                "download_first_samples_per_run": warmup_download,
                                "annotations": "capture.annotations joined by kind and sample_id; warmup:true rows also honored",
                                "combination": "Union of exclusions; a sample is never removed twice."},
              "limits": [
                  "Samples are transformed within their own capture clocks; only derived durations and intervals are pooled.",
                  "Display submission is not full panel transfer or visible-pixel timing.",
                  "Local Supabase persistence acknowledgement is a commit upper bound, not an exact server commit timestamp.",
                  "Download commit latency remains an interval; no midpoint is treated as an exact observation.",
                  "The explicit drift allowance is an engineering assumption; oscillator accuracy is not qualified.",
                  "These are instrumented, descriptive nearest-rank percentiles; small samples do not establish tail reliability.",
                  "Local feedback timings can include rejected/incomplete attempts when that endpoint was reached; upload acknowledgement requires acceptance.",
              ]}
    totals = {"runs": len(captures), "complete_runs": 0, "raw_local": 0, "raw_download": 0,
              "retained_local": 0, "retained_download": 0, "warmup_local": 0, "warmup_download": 0,
              "dropped_device_samples": 0, "host_rpc_records": 0, "host_rpc_duplicates": 0,
              "host_rpc_rejected_or_unknown": 0, "host_rpc_without_device_sample": 0}
    assumptions, source_hashes = [], set()
    requested = None
    for run_id, capture in zip(names, captures):
        device = capture.get("device", capture)
        host = capture.get("host", {})
        annotations = _annotations(capture)
        raw_local = device.get("local", [])
        raw_download = device.get("downloads", [])
        local, removed_local = _filter_rows(raw_local, "local", warmup_local, annotations)
        remote, removed_remote = _filter_rows(raw_download, "download", warmup_download, annotations)
        filtered = {**device, "local": local, "downloads": remote}
        drift = clock_drift_ppm if clock_drift_ppm is not None else capture.get("clock_drift_ppm")
        report = summarize(filtered, host, drift)
        requested = report["requested_metrics"]
        if drift not in assumptions:
            assumptions.append(drift)
        harness = device.get("harness", {})
        source_hash = harness.get("source_sha256")
        if source_hash:
            source_hashes.add(source_hash)
        status = capture.get("capture", {}).get("status", "unspecified")
        all_keys = {("local", row["sample_id"]) for row in raw_local}
        all_keys.update(("download", row["sample_id"]) for row in raw_download)
        missing_annotations = [{"kind": kind, "sample_id": identity}
                               for kind, identity in annotations if (kind, identity) not in all_keys]
        host_rows = list(indexed(host.get("download_rpc", []), "RPC").values())
        downloaded_ids = {row["sample_id"] for row in raw_download}
        run = {"run_id": run_id, "capture_status": status,
               "instrumentation_source_sha256": source_hash,
               "clock_drift_ppm_assumption": drift,
               "raw_sample_counts": {"local": len(raw_local), "download": len(raw_download)},
               "warmup_exclusions": removed_local + removed_remote,
               "annotations_without_device_sample": missing_annotations,
               "report": report}
        result["runs"].append(run)
        totals["complete_runs"] += status == "complete"
        totals["raw_local"] += len(raw_local)
        totals["raw_download"] += len(raw_download)
        totals["retained_local"] += len(local)
        totals["retained_download"] += len(remote)
        totals["warmup_local"] += len(removed_local)
        totals["warmup_download"] += len(removed_remote)
        totals["dropped_device_samples"] += report["dropped_device_samples"]
        totals["host_rpc_records"] += len(host_rows)
        totals["host_rpc_duplicates"] += sum(row.get("duplicate") is True for row in host_rows)
        totals["host_rpc_rejected_or_unknown"] += sum(row.get("accepted") is not True for row in host_rows)
        totals["host_rpc_without_device_sample"] += sum(row["sample_id"] not in downloaded_ids for row in host_rows)
        for metric, (stage, _description) in LOCAL_METRICS.items():
            for row in local:
                if stage not in row or (row.get("outcome") != "accepted" and metric not in FEEDBACK_METRICS):
                    continue
                # summarize() already validated stage/start ordering on this
                # exact filtered run. Keep a derived duration, never raw clocks.
                duration = row[stage] - row["queued_us"]
                local_samples[metric].append({**_sample_key(run_id, "local", row, annotations),
                                              "milliseconds": duration / 1000})
        for metric in DOWNLOAD_METRICS:
            for row in report["downloads"][metric]["samples"]:
                remote_samples[metric].append({**_sample_key(run_id, "download", row, annotations),
                                               "lower_ms": row["lower_ms"], "upper_ms": row["upper_ms"]})
    for metric, (_stage, description) in LOCAL_METRICS.items():
        samples = local_samples[metric]
        result["local"][metric] = {"description": description,
            **statistics([sample["milliseconds"] * 1000 for sample in samples]), "samples": samples}
    for metric, (_stage, description) in DOWNLOAD_METRICS.items():
        samples = remote_samples[metric]
        result["downloads"][metric] = {"description": description, "count": len(samples),
            "lower_bounds": statistics([sample["lower_ms"] * 1000 for sample in samples]),
            "upper_bounds": statistics([sample["upper_ms"] * 1000 for sample in samples]),
            "interval_widths": statistics([(sample["upper_ms"] - sample["lower_ms"]) * 1000 for sample in samples]),
            "samples": samples}
    result.update(totals=totals, requested_metrics=requested,
                  clock_drift_ppm_assumptions=assumptions,
                  instrumentation_source_sha256s=sorted(source_hashes))
    if len(source_hashes) > 1:
        result["limits"].append("Captures used different instrumentation source hashes; inspect each run before comparing or publishing the pooled result.")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--warmup-local", type=int, default=0, help="Exclude this many first local attempts per capture, in addition to annotations")
    parser.add_argument("--warmup-download", type=int, default=0, help="Exclude this many first download attempts per capture, in addition to annotations")
    parser.add_argument("--clock-drift-ppm", type=float, help="Explicit assumption overriding each capture's drift allowance")
    args = parser.parse_args(argv)
    sources = [path.read_bytes() for path in args.captures]
    result = summarize_runs([json.loads(source) for source in sources],
                            names=[str(path) for path in args.captures],
                            warmup_local=args.warmup_local, warmup_download=args.warmup_download,
                            clock_drift_ppm=args.clock_drift_ppm)
    for run, source in zip(result["runs"], sources):
        run["capture_sha256"] = hashlib.sha256(source).hexdigest()
    encoded = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(encoded)
    print(encoded, end="")


if __name__ == "__main__":
    main()
