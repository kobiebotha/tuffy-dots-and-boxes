"""Summarize an exported latency probe without accessing a device or network.

The optional host file contains sync_probes and download_rpc from latency_host.py.
All times are integer microseconds on monotonic clocks. Clock drift is an explicit
assumption, not a claim about qualified oscillator accuracy. No wall clock is used.
"""
import argparse
import json
import math
from pathlib import Path


LOCAL_METRICS = {
    "local_durable_pending": ("pending_saved_us", "Local write → durable pending.json save"),
    "local_framebuffer": ("framebuffer_us", "Local write → framebuffer draw complete"),
    "local_panel_submission": ("panel_submitted_us", "Local write → display.update return"),
    "local_upload_ack_upper_bound": ("rpc_done_us", "Local write → accepted Supabase RPC acknowledgement (commit upper bound)"),
    "local_sqlite_roundtrip": ("sqlite_persisted_us", "Local write → own UUID persisted in synced SQLite table (upload/download roundtrip)"),
    "local_confirmed_framebuffer": ("confirmed_framebuffer_us", "Local write → confirmed framebuffer draw complete"),
    "local_confirmed_panel_submission": ("confirmed_panel_submitted_us", "Local write → confirmed display.update return"),
}
DOWNLOAD_METRICS = {
    "download_sqlite": ("sqlite_persisted_us", "Supabase commit → relevant SQLite table persisted"),
    "download_framebuffer": ("framebuffer_us", "Supabase commit → relevant framebuffer draw complete"),
    "download_panel_submission": ("panel_submitted_us", "Supabase commit → relevant display.update return"),
}


def timestamp(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(label + " must be a nonnegative integer in microseconds")
    return value


def percentile(values, fraction):
    """Nearest-rank percentiles keep observations visible for small samples."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def statistics(values):
    if not values:
        return {"count": 0}
    return {"count": len(values), "min_ms": min(values) / 1000,
            "p50_ms": percentile(values, .50) / 1000,
            "p95_ms": percentile(values, .95) / 1000,
            "max_ms": max(values) / 1000}


def host_interval(device_us, probes, drift_ppm):
    """Map one device timestamp to host time using bounded probe RTT and drift.

    A probe brackets when the device clock was read, so offset is [send-d,recv-d].
    Drift expands each bracket with distance from that read. Intersect all probe
    constraints; an empty intersection is evidence the supplied model is invalid.
    """
    timestamp(device_us, "device timestamp")
    if not probes:
        raise ValueError("clock synchronization probes are missing")
    if isinstance(drift_ppm, bool) or not isinstance(drift_ppm, (int, float)) or not math.isfinite(drift_ppm) or drift_ppm < 0:
        raise ValueError("clock_drift_ppm must explicitly specify a finite nonnegative drift assumption")
    lower, upper = -math.inf, math.inf
    for probe in probes:
        sent = timestamp(probe["host_send_us"], "probe host_send_us")
        received = timestamp(probe["host_receive_us"], "probe host_receive_us")
        device = timestamp(probe["device_us"], "probe device_us")
        if received < sent:
            raise ValueError("clock probe receive precedes send")
        allowance = math.ceil(abs(device_us - device) * drift_ppm / 1_000_000)
        lower = max(lower, sent - device - allowance)
        upper = min(upper, received - device + allowance)
    if lower > upper:
        raise ValueError("clock probe intervals are inconsistent with the drift assumption")
    return device_us + lower, device_us + upper


def commit_latency_interval(device_us, rpc, probes, drift_ppm):
    """Commit occurred somewhere inside the successful nonduplicate RPC RTT.

    Zero is a valid lower bound when the response arrives after the device event.
    Never replace that unknown point with a midpoint or a server wall-clock field.
    """
    sent = timestamp(rpc["host_send_us"], "RPC host_send_us")
    received = timestamp(rpc["host_receive_us"], "RPC host_receive_us")
    if received < sent:
        raise ValueError("RPC receive precedes send")
    event_lower, event_upper = host_interval(device_us, probes, drift_ppm)
    if event_upper < sent:
        raise ValueError("device event precedes the request that should have caused it")
    return max(0, event_lower - received), event_upper - sent


def indexed(rows, label):
    result = {}
    for row in rows:
        identity = timestamp(row.get("sample_id"), label + " sample_id")
        if identity in result:
            raise ValueError("duplicate " + label + " sample_id")
        result[identity] = row
    return result


def summarize(device, host=None, clock_drift_ppm=None):
    if device.get("format_version") != 1:
        raise ValueError("unsupported device format_version")
    host = host or {}
    if host and host.get("format_version") != 1:
        raise ValueError("unsupported host format_version")
    local = indexed(device.get("local", []), "local")
    downloads = indexed(device.get("downloads", []), "download")
    rpcs = indexed(host.get("download_rpc", []), "RPC")
    probes = host.get("sync_probes", [])
    report = {
        "format_version": 1,
        "local_sqlite_direct": {"available": False,
            "reason": "Normal app moves persist pending.json, not SQLite. Synced SQLite measures the upload/download roundtrip."},
        "physical_pixels": {"available": False,
            "reason": "Framebuffer completion and display.update return do not establish physical panel scanout or visible pixels."},
        "clock_drift_ppm_assumption": clock_drift_ppm,
        "local": {}, "downloads": {}, "excluded": [],
        "requested_metrics": {
            "local_write_to_paint": "local.local_panel_submission",
            "local_write_to_supabase_persisted": "local.local_upload_ack_upper_bound",
            "supabase_update_to_sqlite": "downloads.download_sqlite",
            "supabase_update_to_paint": "downloads.download_panel_submission",
        },
        "dropped_device_samples": device.get("dropped", 0),
        "limits": [
            "The accepted Supabase RPC response is a commit acknowledgement upper bound, not a server commit timestamp.",
            "Download latency is an interval from host request/response and clock probe bounds; it is not an exact point estimate.",
            "Clock bounds assume both clocks are monotonic and obey the explicitly supplied drift limit; oscillator accuracy is not qualified.",
            "Nearest-rank percentiles summarize observed samples; small samples do not establish tail reliability.",
        ],
    }
    for metric, (stage, description) in LOCAL_METRICS.items():
        values = []
        for identity, row in local.items():
            # Rejections/failures still have useful local feedback timings, but
            # cannot be interpreted as successful backend persistence/roundtrip.
            if row.get("outcome") != "accepted" and metric not in (
                    "local_durable_pending", "local_framebuffer", "local_panel_submission"):
                continue
            if stage not in row:
                continue
            end = timestamp(row[stage], stage)
            start = timestamp(row["queued_us"], "queued_us")
            if end < start:
                raise ValueError("local stage precedes queued_us for sample " + str(identity))
            values.append(end - start)
        report["local"][metric] = {"description": description, **statistics(values)}
    for metric, (stage, description) in DOWNLOAD_METRICS.items():
        rows, lowers, uppers = [], [], []
        for identity, row in downloads.items():
            if stage not in row:
                continue
            rpc = rpcs.get(identity)
            reason = None
            if rpc is None:
                reason = "missing matching host RPC"
            elif rpc.get("accepted") is not True or rpc.get("duplicate") is not False:
                reason = "RPC was unsuccessful or duplicate; commit origin is unknown"
            else:
                try:
                    lower, upper = commit_latency_interval(row[stage], rpc, probes, clock_drift_ppm)
                except (ValueError, KeyError) as error:
                    reason = str(error)
            if reason:
                report["excluded"].append({"sample_id": identity, "metric": metric, "reason": reason})
                continue
            rows.append({"sample_id": identity, "lower_ms": lower / 1000, "upper_ms": upper / 1000})
            lowers.append(lower)
            uppers.append(upper)
        report["downloads"][metric] = {"description": description,
            "count": len(rows), "lower_bounds": statistics(lowers),
            "upper_bounds": statistics(uppers), "samples": rows}
    report["sample_counts"] = {"local": len(local), "downloads": len(downloads),
                              "local_outcomes": {outcome: sum(row.get("outcome") == outcome for row in local.values())
                                  for outcome in ("accepted", "rejected", "incomplete", "failed")}}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("device", type=Path, help="Probe export JSON, or a combined {device,host,clock_drift_ppm} JSON")
    parser.add_argument("--host", type=Path, help="Optional host probe/RPC JSON")
    parser.add_argument("--clock-drift-ppm", type=float, help="Explicit engineering assumption; not measured oscillator accuracy")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = json.loads(args.device.read_text())
    device = source.get("device", source)
    host = json.loads(args.host.read_text()) if args.host else source.get("host")
    drift = args.clock_drift_ppm if args.clock_drift_ppm is not None else source.get("clock_drift_ppm")
    result = json.dumps(summarize(device, host, drift), indent=2, allow_nan=False) + "\n"
    if args.output:
        args.output.write_text(result)
    print(result, end="")


if __name__ == "__main__":
    main()
