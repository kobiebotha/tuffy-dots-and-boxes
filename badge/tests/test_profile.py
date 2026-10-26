"""The optional RAM probe stays bounded and never changes move outcomes."""
from pathlib import Path
import gc
import sys
from types import SimpleNamespace
import unittest
import weakref

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from profile_badge import Trace, STAGES, QUEUED, SAVED, RPC_DONE, DOWNLOADED, APPLY_BEGIN, COMMITTED, CLEARED, RENDERED, attach


class Clock:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.calls * 10


class ProfileTests(unittest.TestCase):
    def test_fixed_capacity_stops_timer_reads_and_allocating_records(self):
        clock = Clock()
        trace = Trace(2, clock)
        stamps = trace.stamps
        for _ in range(2):
            self.assertTrue(trace.begin())
            for stage in range(1, len(STAGES)):
                trace.mark(stage)
            trace.finish()
        calls = clock.calls
        for _ in range(100):
            self.assertFalse(trace.begin())
            trace.mark(COMMITTED)
        self.assertEqual(clock.calls, calls)
        self.assertIs(trace.stamps, stamps)
        self.assertEqual(len(stamps), 16)
        self.assertEqual(trace.count, 2)
        self.assertEqual(trace.dropped, 100)
        self.assertEqual(trace.rows()[0]['rendered_us'], 70)

    def test_timer_rollover_uses_supplied_ticks_diff(self):
        values = iter((250, 3))
        trace = Trace(1, lambda: next(values), lambda a, b: ((a - b + 128) % 256) - 128)
        trace.begin()
        trace.mark(SAVED)
        trace.finish()
        self.assertEqual(trace.rows()[0]['pending_saved_us'], 9)

    def test_first_mark_and_latest_mark_have_explicit_semantics(self):
        trace = Trace(1, Clock())
        trace.begin()
        trace.mark(DOWNLOADED)
        trace.mark(DOWNLOADED)
        trace.mark(COMMITTED)
        trace.mark(COMMITTED, latest=True)
        trace.finish()
        self.assertEqual(trace.rows()[0]['download_received_us'], 10)
        self.assertEqual(trace.rows()[0]['committed_us'], 30)

    def fixture(self):
        class Records:
            def __init__(self):
                self.record = {'pending_move': None}
            def save(self, record):
                self.record = record
        class Store:
            def control_lines(self, lines):
                return ['committed']
        class Stream:
            def step(self):
                return b'checkpoint'
        class Sync:
            def __init__(self):
                self.stream = Stream()
            def _after_upload(self, accepted=False):
                return accepted
            def step(self, elapsed):
                return self.stream.step()
        class View:
            def draw(self, controller):
                return True
        class Client:
            def __init__(self):
                self.records, self.store, self.sync = Records(), Store(), Sync()
                self.controller = SimpleNamespace(on_claim=self._queue_move)
            def _queue_move(self, preview):
                if self.records.record['pending_move'] is not None:
                    return False
                self.records.save({'pending_move': {'id': 'private-test-id'}})
                return True
        return Client(), View()

    def test_attach_captures_only_timings_and_detach_restores_behavior(self):
        client, view = self.fixture()
        original_claim = client.controller.on_claim
        trace, detach = attach(client, view, Trace(2, Clock()))
        self.assertTrue(client.controller.on_claim(('h', 0, 0)))
        self.assertTrue(client.sync._after_upload(accepted=True))
        self.assertEqual(client.sync.step(16), b'checkpoint')
        self.assertEqual(client.store.control_lines(['raw']), ['committed'])
        client.records.save({'pending_move': None})
        self.assertTrue(view.draw(client.controller))
        self.assertEqual(trace.count, 1)
        self.assertEqual(trace.masks[0], 255)
        row = trace.rows()[0]
        self.assertEqual(row['outcome'], 1)
        self.assertEqual(row['pending_saved_us'], 10)
        self.assertEqual(row['rendered_us'], 70)
        self.assertNotIn('private-test-id', repr(row))
        detach()
        self.assertEqual(client.controller.on_claim, original_claim)
        self.assertNotIn('step', client.sync.__dict__)
        self.assertNotIn('step', client.sync.stream.__dict__)
        self.assertTrue(client.controller.on_claim(('h', 0, 0)))
        self.assertEqual(trace.count, 1)

    def test_refused_and_failed_moves_keep_original_result_and_exception(self):
        client, view = self.fixture()
        trace, detach = attach(client, view, Trace(2, Clock()))
        client.records.record['pending_move'] = {'id': 'already-saved'}
        self.assertFalse(client.controller.on_claim(('h', 0, 0)))
        self.assertEqual(trace.count, 0)
        self.assertEqual(trace.active, -1)
        detach()
        client.records.record['pending_move'] = None
        def fail(_value):
            raise OSError('save failed')
        client.records.save = fail
        trace, detach = attach(client, view, Trace(2, Clock()))
        with self.assertRaisesRegex(OSError, 'save failed'):
            client.controller.on_claim(('h', 0, 0))
        self.assertEqual(trace.rows(), [{'outcome': 3, 'queued_us': 0}])
        self.assertIsNone(client.records.record['pending_move'])
        detach()

    def test_existing_saved_move_requires_its_original_unmeasured_recovery(self):
        client, view = self.fixture()
        client.records.record['pending_move'] = {'id': 'already-saved'}
        with self.assertRaises(ValueError):
            attach(client, view)

    def test_later_batches_cannot_overwrite_the_confirming_commit(self):
        client, view = self.fixture()
        trace, detach = attach(client, view, Trace(2, Clock()))
        client.controller.on_claim(('h', 0, 0))
        client.store.control_lines([])
        committed = trace.stamps[COMMITTED]
        client.records.save({'pending_move': None})
        client.store.control_lines([])
        view.draw(client.controller)
        self.assertEqual(trace.stamps[COMMITTED], committed)
        detach()

    def test_reconnections_release_old_streams_and_capacity_stops_read_hooks(self):
        client, view = self.fixture()
        trace, detach = attach(client, view, Trace(1, Clock()))
        stream_class = type(client.sync.stream)
        previous = []
        for _ in range(100):
            previous.append(weakref.ref(client.sync.stream))
            client.sync.step(16)
            client.sync.stream = stream_class()
        client.sync.step(16)
        gc.collect()
        self.assertTrue(all(item() is None for item in previous))
        client.controller.on_claim(('h', 0, 0))
        client.records.save({'pending_move': None})
        view.draw(client.controller)
        client.sync.step(16)
        self.assertNotIn('step', client.sync.stream.__dict__)
        client.sync.stream = stream_class()
        client.sync.step(16)
        self.assertNotIn('step', client.sync.stream.__dict__)
        detach()

    def test_without_view_finishes_after_step_and_preserves_rejection_outcome(self):
        for accepted in (True, False):
            client, _view = self.fixture()
            def complete(_elapsed):
                client.records.save({'pending_move': None})
                client.sync._after_upload(accepted=accepted)
                return 'finished'
            client.sync.step = complete
            trace, detach = attach(client, trace=Trace(2, Clock()))
            for _ in range(2):
                self.assertTrue(client.controller.on_claim(('h', 0, 0)))
                self.assertEqual(client.sync.step(16), 'finished')
                self.assertEqual(trace.active, -1)
            self.assertEqual([row['outcome'] for row in trace.rows()], [1 if accepted else 2] * 2)
            self.assertTrue(all('rendered_us' not in row for row in trace.rows()))
            detach()


if __name__ == '__main__':
    unittest.main()
