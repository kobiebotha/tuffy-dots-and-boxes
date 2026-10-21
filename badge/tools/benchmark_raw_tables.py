#!/usr/bin/env python3
"""Current compact projection, cache32, actual production shadow VFS, pinned core.

Read-only source imports; only temporary POSIX databases and local report written.
Host/Python timings are not MicroPython/LittleFS or radio measurements.
"""
import argparse
import base64
from collections import Counter
import json
from pathlib import Path
import statistics
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'badge/tools'), str(ROOT / 'badge/tests')]
import benchmark_native as bench
from dots_codec import decode_edge
from game import Game
from vfs_fixture import PythonVFSCore

original_store = bench.storage.Store
original_vfs = bench.storage.BadgeVFS
physical = Counter()
checkpoint_io = []
checkpoint_physical = []
reopen_results = {}
last_native = Counter()
last_physical = Counter()
inputs = None
want_ui = False


def verify_full_game(actual):
    fixture = next(item for item in inputs['fixtures'] if item['name'] == 'v2_safe_r')
    source = [json.loads(op['data']) for op in fixture['scenarios']['fresh']['ops']
              if op['object_type'] == 'moves']
    expected = []
    for move in sorted(source, key=lambda item: item['n']):
        orientation, row, col = decode_edge(move['e'])
        expected.append({'orientation': orientation, 'row': row, 'col': col,
                         'move_index': move['n']})
    assert actual == expected, (actual, expected)
    game = Game()
    for move in actual:
        assert game.claim(move['orientation'], move['row'], move['col']) is not None
    assert game.finished and game.scores == [10, 10] and sum(bool(box) for box in game.boxes) == 20


class CountedVFS(original_vfs):
    def _sync_db(self, stream):
        physical['db.sync'] += 1
        if self.db_dirty:
            physical['db.write'] += 1
            physical['db.writeBytes'] += self.db_size
        return super()._sync_db(stream)

class CapturedStore(original_store):
    def start_sync(self, *args, **kwargs):
        result = super().start_sync(*args, **kwargs)
        self.native.reset_stats()
        physical.clear()
        checkpoint_io.clear()
        checkpoint_physical.clear()
        last_native.clear()
        last_physical.clear()
        return result

    def control_lines(self, payloads):
        result = super().control_lines(payloads)
        if any('checkpoint_complete' in json.loads(line) for line in payloads):
            if want_ui:
                self.room(inputs['roomId'])
                self.moves(inputs['roomId'])
                for line in payloads:
                    for op in json.loads(line).get('data', {}).get('data', []):
                        if op.get('op') == 'PUT' and op.get('object_type') == 'moves':
                            canonical = str(uuid.UUID(bytes=base64.b64decode(op['object_id'])))
                            assert self.has_move(canonical, inputs['roomId'])
            now = Counter(self.native.stats)
            checkpoint_io.append(dict(now - last_native))
            last_native.clear()
            last_native.update(now)
            checkpoint_physical.append(dict(physical - last_physical))
            last_physical.clear()
            last_physical.update(physical)
        return result

    def close(self):
        qualify = self.opened
        if qualify:
            expected_room = self.room(inputs['roomId'])
            expected_moves = self.moves(inputs['roomId'])
            assert len(expected_moves) == 49, len(expected_moves)
            verify_full_game(expected_moves)
            assert expected_room['move_count'] == 49
            assert expected_room['score_p1'] == 10 and expected_room['score_p2'] == 10
            assert self.native.exec('PRAGMA quick_check') == [['ok']]
        super().close()
        if qualify:
            reopened = original_store(native=PythonVFSCore(), root=self.root)
            started = time.perf_counter()
            reopened.open()
            open_ms = (time.perf_counter() - started) * 1000
            try:
                reopened.native.reset_stats()
                physical.clear()
                started = time.perf_counter()
                assert reopened.room(inputs['roomId']) == expected_room
                assert reopened.moves(inputs['roomId']) == expected_moves
                assert not reopened.has_local_writes()
                query_ms = (time.perf_counter() - started) * 1000
                assert not reopened.bind_account(inputs['userId'])
                reopen_results.update({'passed': True, 'moves': len(expected_moves),
                    'openMilliseconds': round(open_ms, 4), 'queryMilliseconds': round(query_ms, 4),
                    'queryIo': dict(reopened.native.stats), 'physical': dict(physical)})
            finally:
                original_store.close(reopened)

def main():
    global inputs, want_ui
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fixtures', help='Service fixture JSON or --service-only benchmark report')
    parser.add_argument('--output', required=True)
    parser.add_argument('--repetitions', type=int, default=3)
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error('--repetitions must be positive')
    fixtures_path, report_path = args.fixtures, args.output
    inputs = json.loads(Path(fixtures_path).read_text())
    if 'fixtures' not in inputs:
        fixtures_path = inputs['methodology']['fixtures']
        inputs = json.loads(Path(fixtures_path).read_text())
    names = ('v2_safe_r', 'v2_raw_moves', 'v2_raw_all')
    fixtures = [next(f for f in inputs['fixtures'] if f['name'] == name) for name in names]
    # Fail if a candidate's wire protocol differs: this compares storage only.
    for fixture in fixtures[1:]:
        assert fixture['scenarios'] == fixtures[0]['scenarios']
    bench.HostCore = PythonVFSCore
    bench.storage.Store = CapturedStore
    bench.storage.BadgeVFS = CountedVFS
    results = []
    for rep in range(args.repetitions):
        # Alternate candidate order so warm filesystem effects do not privilege one.
        for fixture in fixtures[::1 if rep % 2 == 0 else -1]:
            for scenario_name in ('fresh', 'history_cold', 'history_live', 'history_live_ui'):
                want_ui = scenario_name.endswith('_ui')
                reopen_results.clear()
                scenario = fixture['scenarios']['history_live' if want_ui else scenario_name]
                result = bench.run(fixture, scenario_name, scenario, inputs['userId'])
                result.update({'repetition': rep, 'io': dict(last_native),
                    'physical': dict(last_physical), 'checkpointIo': list(checkpoint_io),
                    'checkpointPhysical': list(checkpoint_physical), 'reopen': dict(reopen_results)})
                results.append(result)
                print(json.dumps({k:v for k,v in result.items() if k not in ('checkpointIo','checkpointPhysical')}), flush=True)
    summaries = []
    for name in names:
        for scenario in ('fresh', 'history_cold', 'history_live', 'history_live_ui'):
            cases = [r for r in results if r['variant'] == name and r['scenario'] == scenario]
            assert not any('error' in r or 'closeError' in r for r in cases), cases
            summaries.append({'variant': name, 'scenario': scenario, 'runs': len(cases),
                'databaseBytes': cases[0]['databaseBytes'],
                'maxSyncPeakBytes': max(r['syncPeakBytes'] for r in cases),
                'maxSetupPeakBytes': max(r['setupPeakBytes'] for r in cases),
                'medianHostMilliseconds': statistics.median(r['hostMilliseconds'] for r in cases),
                'hostMillisecondsRange': [min(r['hostMilliseconds'] for r in cases),max(r['hostMilliseconds'] for r in cases)],
                'io': {key: statistics.median(r['io'].get(key,0) for r in cases) for key in sorted(set().union(*(r['io'] for r in cases)))},
                'physical': {key: statistics.median(r['physical'].get(key,0) for r in cases) for key in sorted(set().union(*(r['physical'] for r in cases)))},
                'medianReopenMilliseconds': statistics.median(r['reopen']['openMilliseconds'] for r in cases),
                'medianReopenQueryMilliseconds': statistics.median(r['reopen']['queryMilliseconds'] for r in cases),
                'all49MovesAndFullScoreReopenPassed': all(r['reopen'].get('passed') for r in cases)})
    # Keep totals and checkpoint ranges instead of dumping every callback.
    for result in results:
        checkpoints = result.pop('checkpointIo')
        snapshots = result.pop('checkpointPhysical')
        result['checkpointSyncCounts'] = {
            'minimum': min(p.get('sync',0) for p in checkpoints),
            'median': statistics.median(p.get('sync',0) for p in checkpoints),
            'maximum': max(p.get('sync',0) for p in checkpoints)}
        result['representativeCheckpoint'] = {'index': min(5,len(checkpoints)-1),
            'io': checkpoints[min(5,len(checkpoints)-1)],
            'physical': snapshots[min(5,len(snapshots)-1)]}
    report = {'methodology': {'source': str(ROOT), 'library': str(bench.LIBRARY), 'fixtures': fixtures_path,
        'core': '0.5.4/cratesio', 'arenaBytes': 256*1024, 'cachePages': 32, 'shadowBytes': 128*1024,
        'vfs': 'Production Python BadgeVFS via PythonVFSCore C ABI adapter. Temporary POSIX files.',
        'equivalence': 'All three scenarios use byte-for-byte identical compact service-produced operations, checksums and grouping. Both raw variants retain the account local-only JSON table.',
        'validation': '50 rows; expected published fields; all 49 decoded moves compared with service fixture and replayed through Game for 10-10/20 boxes; identical rows on reopen; quick_check; completed checkpoint count; empty ps_crud.',
        'limitations': 'Fresh creation and reopen/read qualification only. No raw migration, post-reopen sync, account-switch, REMOVE, or fault injection qualification. No MicroPython, LittleFS, radio, device timing or power-loss claims. Native highwater excludes Python heap/shadow buffer; the reserved 256 KiB arena and 128 KiB shadow stay fixed. Physical database counts instrument production _sync_db, not hardware flash programming. Host timings include macOS os.sync and cannot predict Tufty speedup.',
        'reproduce': 'node powersync-cloud/tests/benchmark-projection.mjs --service-only --output=/private/tmp/dots-raw-service.json\npython3 badge/tools/benchmark_raw_tables.py /private/tmp/dots-raw-service.json --repetitions='+str(args.repetitions)+' --output='+report_path},
        'summaries': summaries, 'results': results}
    Path(report_path).write_text(json.dumps(report,indent=2)+'\n')
    print('RESULT '+report_path)

if __name__ == '__main__':
    main()
