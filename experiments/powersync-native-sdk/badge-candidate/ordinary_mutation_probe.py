#!/usr/bin/env python3
"""Generate or host-check an isolated ordinary SDK mutation/watch replay.

Historical performance harness definitions provide meters and the byte fixture;
its old pending/receipt test runners are never invoked. Badge runs use current
installed code. --host checks the same flow with the actual cached SDK ABI and
SQLite but cannot establish badge latency. No network is opened in either mode.
"""
import argparse
from pathlib import Path
import sys
import time

from performance_replay import build_source as historical_source

HERE = Path(__file__).resolve().parent


def build_source(storage='memory', repetitions=1, pace_ms=16, root='/state/dots_boxes_benchmark'):
    source = historical_source('native', storage=storage, scenario='warm_upload',
                               repetitions=repetitions, pace_ms=pace_ms, root=root)
    marker = '\nif BENCH_CONFIG.get("keep_stream", False):'
    assert source.count(marker) == 1
    return source.split(marker)[0] + '\n' + (HERE / 'ordinary_mutation_payload.py').read_text()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--storage', choices=('memory', 'flash'), default='memory')
    parser.add_argument('--root', default='/state/dots_boxes_benchmark')
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--pace-ms', type=int, default=16)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--host', action='store_true')
    args = parser.parse_args()
    if args.host and args.storage != 'memory':
        parser.error('Host checks are memory-only; flash means the actual badge VFS')
    if args.host and args.repetitions != 1:
        parser.error('The cached host shim initializes SQLite once; repeat host checks in separate processes')
    source = build_source(args.storage, args.repetitions, args.pace_ms, args.root)
    if args.output:
        args.output.write_text(source)
        print(args.output)
    if args.host:
        from performance_host_replay import HostNative
        app = HERE / 'apps/dots_boxes_native'
        sys.path[:0] = [str(app), str(app / 'powersync')]
        import powersync.runtime
        powersync.runtime.load_native = lambda heartbeat=None: HostNative()
        time.ticks_us = lambda: time.perf_counter_ns() // 1000
        time.ticks_ms = lambda: time.perf_counter_ns() // 1000000
        time.ticks_diff = lambda a, b: a - b
        time.sleep_ms = lambda milliseconds: time.sleep(milliseconds / 1000)
        scope = {'BENCH_HEARTBEAT': lambda: None, 'ORDINARY_HOST_CHECK': True}
        exec(compile(source, str(HERE / 'ordinary_mutation_payload.py'), 'exec'), scope)
    elif not args.output:
        parser.error('Use --output to generate a badge payload, or --host')


if __name__ == '__main__':
    main()
