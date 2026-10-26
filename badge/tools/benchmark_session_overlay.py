#!/usr/bin/env python3
"""Count real session-file bytes for one queued/confirmed move, without a badge."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "sqlite-shadow-vfs"), str(ROOT / "badge/lib"), str(ROOT / "badge/app/dots_boxes")]


def load(path):
    spec = importlib.util.spec_from_file_location("session_candidate", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(module, overlay):
    with tempfile.TemporaryDirectory(prefix="dots-session-benchmark-") as root:
        session = module.SessionFile(root)
        saved = dict(module.fresh_session("11111111-1111-4111-8111-111111111111"),
            user_id="22222222-2222-4222-8222-222222222222",
            room_id="33333333-3333-4333-8333-333333333333", seat=1,
            refresh_token="r" * 512, access_token="a" * 2048, expires_at=10000)
        session.save(saved)
        original = Path(session.path).read_bytes()
        writes = []
        for desired in (dict(saved, pending_move={"id": "44444444-4444-4444-8444-444444444444",
                         "orientation": "h", "row": 0, "col": 1}), saved):
            session.save(desired)
            writes.append(Path(session.pending_path if overlay else session.path).stat().st_size)
            assert module.SessionFile(root).load(None) == desired
        return {"moveWriteBytes": writes, "totalBytes": sum(writes),
                "baseBytes": len(original), "baseUnchanged": Path(session.path).read_bytes() == original,
                "restartPendingAndClearPassed": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    current = ROOT / "badge/app/dots_boxes/dots_session.py"
    before, after = run(load(args.baseline), False), run(load(current), True)
    report = {"method": "Real SessionFile saves to temporary POSIX files; unchanged flush/sync/rename/sync ordering. Synthetic credentials: 2048-byte access token, 512-byte refresh token. Byte counts measure saved UTF-8 contents, not badge flash programming or latency.",
              "baselineSource": str(args.baseline), "baselineSha256": hashlib.sha256(args.baseline.read_bytes()).hexdigest(),
              "candidateSha256": hashlib.sha256(current.read_bytes()).hexdigest(),
              "before": before, "after": after,
              "savedBytesPercent": round(100 * (1 - after["totalBytes"] / before["totalBytes"]), 2)}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
