#!/usr/bin/env python3
"""Aggregate badge performance logs without turning nested timers into totals.

Usage: python3 performance_results.py [--directory DIR] [--output FILE]
Only PERFORMANCE_{REPLAY,LIVE,RENDER}_RESULT JSON records are consumed. USB REPL
control characters and surrounding boot/diagnostic output are ignored. Missing
or incomplete records are reported so running/failed logs cannot masquerade as
successful samples. Re-run after all hardware runs finish.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import statistics


HERE = Path(__file__).resolve().parent
MARKER = re.compile(r"PERFORMANCE_(REPLAY|LIVE|RENDER)_RESULT\s*")
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
OP_NAMES = ("open", "close", "read", "write", "truncate", "sync", "size", "delete", "access", "random", "time")


def parse_records(raw, source):
    cleaned = CONTROL.sub("", raw)
    records, warnings = [], []
    decoder = json.JSONDecoder()
    for index, marker in enumerate(MARKER.finditer(cleaned)):
        try:
            value, _ = decoder.raw_decode(cleaned, marker.end())
            if not isinstance(value, dict):
                raise ValueError("result must be a JSON object")
            records.append({"kind": marker.group(1).lower(), "source": source,
                            "source_record": index, "result": value})
        except (ValueError, json.JSONDecodeError) as error:
            warnings.append({"source": source, "source_record": index,
                             "error": str(error)})
    return records, warnings


def summary(values):
    values = list(values)
    return {"n": len(values), "min": min(values), "median": statistics.median(values), "max": max(values)}


def numeric(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def flatten(value, prefix=""):
    result = {}
    for key, item in value.items():
        name = prefix + key
        if numeric(item):
            result[name] = item
        elif isinstance(item, dict):
            result.update(flatten(item, name + "."))
    return result


def measured_metrics(value):
    result = flatten(value)
    vfs = value.get("vfs") or {}
    native = value.get("native") or {}
    calls_us = sum(native.get(name, 0) for name in (
        "exec_us_inclusive", "sdk_poll_us_inclusive", "sdk_start_us", "sdk_reply_us"))
    if native:
        result["derived.native_calls_us_inclusive"] = calls_us
    if native and "callback_total_us" in vfs:
        result["derived.native_calls_minus_vfs_callback_us"] = calls_us - vfs["callback_total_us"]
        if calls_us:
            result["derived.vfs_fraction_of_native_calls"] = vfs["callback_total_us"] / calls_us
    for name in ("max_step_us", "frame_max_us"):
        if numeric(value.get(name)):
            result["derived.max_foreground_stall_us"] = value[name]
            break
    if numeric(value.get("rpc_accepted_us")) and numeric(value.get("pending_cleared_us")):
        result["derived.rpc_accepted_to_pending_cleared_us"] = value["pending_cleared_us"] - value["rpc_accepted_us"]
    if numeric(value.get("wall_us")) and numeric(value.get("explicit_pace_wait_us")):
        result["derived.wall_minus_explicit_pace_wait_us"] = value["wall_us"] - value["explicit_pace_wait_us"]
    if numeric(value.get("wall_us")) and value.get("last_chunk_us", 0) > 0:
        result["derived.last_chunk_to_finished_us"] = value["wall_us"] - value["last_chunk_us"]
    # These arrays may include validation/shutdown in older logs: preserve as
    # diagnostic metrics, never recompute the scalar callback_total_us from them.
    for array, label in (("callback_counts", "diagnostic_callback_count"), ("callback_us", "diagnostic_callback_us")):
        for op, number in zip(vfs.get("op_names", OP_NAMES), vfs.get(array, [])):
            result["vfs." + label + "." + op] = number
    return result


def request_kind(event):
    path = event.get("path", "").split("?", 1)[0]
    if path.endswith("/sync/stream"):
        return "sync_stream"
    if path.endswith("/sync/checkpoint-request"):
        return "checkpoint_request"
    if "auth" in path or "token" in path:
        return "auth"
    if "move" in path or "claim" in path:
        return "move_rpc"
    return path or "unknown"


def http_metrics(value):
    result = defaultdict(float)
    phases_cpu, phases_wall, phases_wall_with_final = defaultdict(int), defaultdict(int), defaultdict(int)
    for event in value.get("http", []):
        kind = request_kind(event)
        prefix = "http." + kind + "."
        result[prefix + "request_count"] += 1
        result[prefix + "body_bytes"] += event.get("bytes", 0)
        result[prefix + "max_call_us"] = max(result[prefix + "max_call_us"], event.get("max_call_us", 0))
        for phase, duration in event.get("cpu_us", {}).items():
            result[prefix + "phase_cpu_us." + phase] += duration
            phases_cpu[phase] += duration
        completed_wall = event.get("wall_us", {})
        for phase, duration in completed_wall.items():
            result[prefix + "completed_phase_wall_us." + phase] += duration
            result[prefix + "phase_wall_with_final_interval_us." + phase] += duration
            phases_wall[phase] += duration
            phases_wall_with_final[phase] += duration
        for phase, count in event.get("calls", {}).items():
            result[prefix + "phase_call_count." + phase] += count
        start, closed = event.get("start_us"), event.get("closed_us")
        if numeric(start) and numeric(closed):
            lifetime = closed - start
            result[prefix + "lifetime_wall_us"] += lifetime
            final_phase = event.get("close_phase", event.get("phase", "unknown"))
            # http_close records an endpoint, while active phase_start was
            # intentionally removed from JSON. Earlier completed intervals are
            # already recorded, so the remainder reconstructs this final interval.
            final_interval = max(0, lifetime - sum(completed_wall.values()))
            result[prefix + "phase_wall_with_final_interval_us." + final_phase] += final_interval
            phases_wall_with_final[final_phase] += final_interval
        first_body = event.get("first_body_us")
        if numeric(start) and numeric(first_body):
            result[prefix + "start_to_first_body_us"] += first_body - start
    streams = sum(request_kind(event) == "sync_stream" for event in value.get("http", []))
    result["http.total_request_count"] = len(value.get("http", []))
    result["http.stream_reconnect_count"] = max(0, streams - 1)
    for phase, duration in phases_cpu.items():
        result["http.all.phase_cpu_us." + phase] = duration
    for phase, duration in phases_wall.items():
        result["http.all.completed_phase_wall_us." + phase] = duration
    for phase, duration in phases_wall_with_final.items():
        result["http.all.phase_wall_with_final_interval_us." + phase] = duration
    return dict(result)


def dimensions(record):
    value = record["result"]
    common = {"variant": value.get("variant", "unknown")}
    if record["kind"] == "replay":
        common.update({key: value.get(key, default) for key, default in (
            ("storage", "unknown"), ("scenario", "cold"), ("pace_ms", 0), ("keep_stream", False),
            ("wire_sha256", None), ("fixture_sha256", None))})
    elif record["kind"] == "live":
        common.update({"scenario": "live_cold_download", "room": value.get("room"),
                       "move_count": value.get("move_count")})
    return common


def group_records(records):
    buckets = defaultdict(list)
    render_buckets = defaultdict(list)
    for record in records:
        if record["kind"] == "render":
            for sample in record["result"].get("samples", []):
                dims = {"variant": record["result"].get("variant", "unknown"), "scene": sample.get("scene", "unknown")}
                render_buckets[json.dumps(dims, sort_keys=True)].append((record, sample))
            continue
        dims = dimensions(record)
        buckets[(record["kind"], json.dumps(dims, sort_keys=True))].append(record)
    groups = []
    for (kind, key), items in sorted(buckets.items()):
        metrics = defaultdict(list)
        for record in items:
            values = measured_metrics(record["result"])
            if kind == "live":
                values.update(http_metrics(record["result"]))
            for name, number in values.items():
                metrics[name].append(number)
        groups.append({"kind": kind, **json.loads(key), "sample_count": len(items),
                       "sources": [{"file": item["source"], "record": item["source_record"],
                                    "repetition": item["result"].get("repetition")} for item in items],
                       "scopes": sorted({item["result"].get("scope", "unspecified") for item in items}),
                       "identity_evidence": [{"file": item["source"], "record": item["source_record"],
                                              "identity": item["result"]["identity"]}
                                             for item in items if item["result"].get("identity")],
                       "raw_state_sha256_values": sorted({item["result"]["raw_state_sha256"]
                                                          for item in items if item["result"].get("raw_state_sha256")}),
                       "all_validated": all(item["result"].get("validated_raw_rows_exact_uuid_room_empty_crud_quick_check", item["result"].get("synced", False)) for item in items),
                       "metrics": {name: summary(numbers) for name, numbers in sorted(metrics.items())}})
    for key, items in sorted(render_buckets.items()):
        metrics = defaultdict(list)
        for _, sample in items:
            for name, number in flatten(sample).items():
                metrics[name].append(number)
        groups.append({"kind": "render", **json.loads(key), "round_sample_count": len(items),
                       "run_count": len({(item[0]["source"], item[0]["source_record"]) for item in items}),
                       "sources": sorted({item[0]["source"] for item in items}),
                       "scopes": sorted({item[0]["result"].get("scope", "unspecified") for item in items}),
                       "initial_framebuffer_sha256": sorted({item[1].get("initial_framebuffer_sha256", "unknown") for item in items}),
                       "final_framebuffer_sha256": sorted({item[1].get("final_framebuffer_sha256", "unknown") for item in items}),
                       "metrics": {name: summary(numbers) for name, numbers in sorted(metrics.items())}})
    return groups


def comparisons(groups):
    buckets = defaultdict(dict)
    dimension_names = ("kind", "storage", "scenario", "pace_ms", "keep_stream", "wire_sha256", "fixture_sha256", "room", "move_count", "scene")
    for group in groups:
        key = json.dumps({name: group[name] for name in dimension_names if name in group}, sort_keys=True)
        buckets[key][group["variant"]] = group
    result = []
    for key, variants in sorted(buckets.items()):
        if not {"original", "native"}.issubset(variants):
            continue
        before, after = variants["original"]["metrics"], variants["native"]["metrics"]
        metric_comparison = {}
        for name in sorted(before.keys() & after.keys()):
            original, native = before[name]["median"], after[name]["median"]
            metric_comparison[name] = {"original_median": original, "native_median": native,
                                       "native_minus_original": native - original,
                                       "native_to_original_ratio": native / original if original else None}
        evidence = {}
        for variant in ("original", "native"):
            group = variants[variant]
            evidence[variant] = {"identity_sample_count": len(group.get("identity_evidence", [])),
                                 "badge_ids": sorted({item["identity"]["badge_id"] for item in group.get("identity_evidence", [])
                                                      if item["identity"].get("badge_id")}),
                                 "raw_state_sha256_values": group.get("raw_state_sha256_values", [])}
        evidence["same_badge_verified_by_identity"] = (len(evidence["original"]["badge_ids"]) == 1
            and evidence["original"]["badge_ids"] == evidence["native"]["badge_ids"])
        evidence["same_raw_state_verified_by_hash"] = (len(evidence["original"]["raw_state_sha256_values"]) == 1
            and evidence["original"]["raw_state_sha256_values"] == evidence["native"]["raw_state_sha256_values"])
        result.append({**json.loads(key), "source_verification": evidence, "metrics": metric_comparison})
    return result


def aggregate(directory):
    all_records, warnings, inputs = [], [], []
    for path in sorted(directory.glob("performance-*.log")):
        raw = path.read_bytes()
        records, errors = parse_records(raw.decode(errors="replace"), path.name)
        all_records.extend(records)
        warnings.extend(errors)
        inputs.append({"file": path.name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                       "result_records": len(records)})
    groups = group_records(all_records)
    return {"format_version": 1, "statistics": "Median, minimum and maximum; no inferred p95 from three repetitions. Renderer per-round p95 values, if present, each describe the payload's own iteration count.",
            "timing_units": "Microseconds for fields ending _us; counts/bytes otherwise. Ratios are dimensionless.",
            "attribution_limits": [
                "Native exec, SDK poll/start/reply and VFS timings are nested. Do not add inclusive timers or database_image_sync_us to VFS callback_total_us.",
                "native_calls_minus_vfs_callback_us includes SDK, core/SQLite CPU, mailbox/argument conversion; it is not pure scheduler CPU.",
                "Older VFS per-op arrays were returned by reference and may include post-window validation/shutdown. Use scalar callback_total_us for measured attribution.",
                "TLS send phase includes handshake and request writes; tcp phase includes SSL setup/CA loading after TCP readiness. No isolated handshake hook exists.",
                "Live HTTP CPU/wall totals span the live payload's complete request lifetime, including post-sync idle frames and shutdown. Completed-phase wall excludes the final ongoing phase; phase_wall_with_final_interval reconstructs its remainder from close timestamps.",
                "Same-room live network runs are sequential and subject to network variance. Compare their raw move_count and request byte counts as well as timings.",
                "Replay excludes network/TLS and rendering. Warm-upload replay uses fixture RPC and RAM session saves while retaining real GameConnector, SDK CRUD and confirmation behavior.",
                "Renderer draw CPU and display.update panel+VSYNC are measured separately; panel transfer cannot be separated from VSYNC without firmware hooks."],
            "inputs": inputs, "parse_warnings": warnings,
            "record_counts": {kind: sum(record["kind"] == kind for record in all_records) for kind in ("replay", "live", "render")},
            "identity_evidence": [{"kind": record["kind"], "source": record["source"],
                                   "source_record": record["source_record"], "variant": record["result"].get("variant"),
                                   "raw_state_sha256": record["result"].get("raw_state_sha256"),
                                   "identity": record["result"]["identity"]}
                                  for record in all_records if record["result"].get("identity")],
            "groups": groups, "comparisons": comparisons(groups), "raw_runs": all_records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=HERE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = aggregate(args.directory)
    output = args.output or args.directory / "performance-results.json"
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(output), "records": result["record_counts"],
                      "groups": len(result["groups"]), "parse_warnings": len(result["parse_warnings"])}))


if __name__ == "__main__":
    main()
