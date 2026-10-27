"""Move latency measurements follow real client save/confirmation boundaries."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "badge/tools"), str(ROOT / "badge/lib"),
                str(ROOT / "badge/app/dots_boxes"), str(ROOT / "sqlite-shadow-vfs")]

from dots_client import Client
from dots_session import fresh_session
from latency_probe import Probe, attach

ROOM = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
DEVICE = "33333333-3333-4333-8333-333333333333"
MOVE = "44444444-4444-4444-8444-444444444444"
OTHER_MOVE = "55555555-5555-4555-8555-555555555555"
OTHER_ROOM = "66666666-6666-4666-8666-666666666666"
EDGE = ("h", 0, 0)


class Clock:
    def __init__(self):
        self.value = 1000

    def __call__(self):
        return self.value

    def advance(self, amount):
        self.value += amount


class Session:
    def __init__(self, clock):
        self.clock = clock
        self.fail = False
        self.record = dict(fresh_session(DEVICE), room_id=ROOM, user_id=USER, seat=1,
                           access_token="secret-access", refresh_token="secret-refresh", expires_at=10**12)

    def load(self, _new_uuid):
        return self.record

    def save(self, record):
        if self.fail:
            raise OSError("durability barrier failed")
        self.clock.advance(30)
        self.record = record


class Store:
    def __init__(self, clock):
        self.clock = clock
        self.history = []
        self.lookup_delay = 0
        self.lookups = []
        self.value = {"status": "playing", "cols": 5, "rows": 4, "current_seat": 1,
                      "score_p1": 0, "score_p2": 0, "winner": None, "move_count": 0}

    def new_uuid(self):
        return MOVE

    def reconcile_session(self, _record):
        pass

    def room(self, _room_id):
        return dict(self.value)

    def moves(self, room_id, after=0):
        return [dict(row) for row in self.history if row["room_id"] == room_id and row["move_index"] > after]

    def has_move(self, move_id, room_id):
        self.lookups.append((move_id, room_id))
        self.clock.advance(self.lookup_delay)
        return any(row["id"] == move_id and row["room_id"] == room_id for row in self.history)

    def control_lines(self, payloads, parsed=None):
        self.clock.advance(100)
        for row in payloads:
            if "id" in row:
                self.history.append(dict(row))
                self.value["move_count"] = max(self.value["move_count"], row["move_index"])
        return parsed or []


class View:
    def __init__(self, clock):
        self.clock = clock
        self.draws = []

    def draw(self, controller, local_seat=None):
        self.clock.advance(20)
        self.draws.append((controller.pending_edge, controller.game.moves))
        return "drawn"


def move(identity=MOVE, room=ROOM, edge=EDGE, index=1):
    return {"id": identity, "room_id": room, "orientation": edge[0], "row": edge[1],
            "col": edge[2], "move_index": index}


class LatencyProbeTests(unittest.TestCase):
    def fixture(self, capacity=4):
        clock = Clock()
        session, store, view = Session(clock), Store(clock), View(clock)
        client = Client(store, session)
        client.sync.synced = True
        client.sync.phase = "stream"
        client.sync.status = "SYNCED"
        client.sync.stream = SimpleNamespace(close=lambda: None)
        client._apply_room()
        probe, detach = attach(client, view, Probe(capacity, clock))
        self.addCleanup(detach)
        return client, store, session, view, probe, clock, detach

    def publish(self, client, store, rows, full=True, completed=True):
        result = {"full_checkpoint": full,
                  "instructions": [{"DidCompleteSync": {}}] if completed else []}
        results = store.control_lines(rows, parsed=[result])
        for item in results:
            client.sync._instructions(item["instructions"], full_checkpoint=item["full_checkpoint"])
        client._apply_room()
        return results

    def test_local_feedback_is_measured_before_upload_and_confirmed_frame_is_separate(self):
        client, store, session, view, probe, clock, _ = self.fixture()
        self.assertTrue(client.controller.on_claim(EDGE))
        self.assertEqual(session.record["pending_move"]["id"], MOVE)
        self.assertEqual(client.controller.pending_edge, EDGE)
        self.assertEqual(view.draw(client.controller), "drawn")
        before_panel = probe.export()["local"][0]
        self.assertEqual(before_panel["framebuffer_us"], 50)
        self.assertNotIn("panel_submitted_us", before_panel)
        self.assertNotIn("rpc_done_us", before_panel)
        clock.advance(16)
        probe.panel_submitted()
        first_panel = probe.export()["local"][0]["panel_submitted_us"]
        clock.advance(1000)
        client.sync._after_upload(accepted=True)
        ack = probe.export()["local"][0]["rpc_done_us"]
        self.assertGreater(ack, first_panel)
        self.assertIsNotNone(probe.active)
        self.publish(client, store, [move()])
        self.assertIsNone(client.records.record["pending_move"])
        view.draw(client.controller)
        clock.advance(16)
        probe.panel_submitted()
        row = probe.export()["local"][0]
        self.assertEqual(row["panel_submitted_us"], first_panel)
        self.assertEqual(row["rpc_done_us"], ack)
        self.assertGreater(row["confirmed_panel_submitted_us"], row["sqlite_persisted_us"])
        self.assertEqual(row["outcome"], "accepted")
        self.assertIsNone(probe.active)

    def test_commit_requires_publication_of_exact_uuid_and_room(self):
        client, store, _, view, probe, _, _ = self.fixture()
        client.controller.on_claim(EDGE)
        self.publish(client, store, [move(identity=OTHER_MOVE)])
        self.assertNotIn("sqlite_persisted_us", probe.export()["local"][0])
        self.publish(client, store, [move(room=OTHER_ROOM)])
        self.assertNotIn("sqlite_persisted_us", probe.export()["local"][0])
        self.publish(client, store, [move()], full=False, completed=False)
        self.assertNotIn("sqlite_persisted_us", probe.export()["local"][0])
        self.publish(client, store, [], completed=False)
        self.assertNotIn("sqlite_persisted_us", probe.export()["local"][0])
        self.publish(client, store, [], full=False)
        committed = probe.export()["local"][0]["sqlite_persisted_us"]
        # The measurement is first published SQLite visibility. The app itself
        # intentionally keeps its pending command until a FULL checkpoint.
        self.assertIsNotNone(client.records.record["pending_move"])
        self.publish(client, store, [])
        self.assertEqual(probe.export()["local"][0]["sqlite_persisted_us"], committed)
        self.assertIsNone(client.records.record["pending_move"])
        self.assertIn((MOVE, ROOM), store.lookups)

    def test_remote_partial_publication_marks_first_persisted_row(self):
        client, store, _, view, probe, _, _ = self.fixture()
        probe.arm_download(client, 9, MOVE, EDGE, 1)
        self.publish(client, store, [move()], full=False)
        committed = probe.export()["downloads"][0]["sqlite_persisted_us"]
        self.publish(client, store, [])
        self.assertEqual(probe.export()["downloads"][0]["sqlite_persisted_us"], committed)
        view.draw(client.controller)
        probe.panel_submitted()
        self.assertEqual(probe.export()["downloads"][0]["outcome"], "accepted")

    def test_sqlite_timestamp_precedes_extra_identity_query_cost(self):
        client, store, _, _, probe, clock, _ = self.fixture()
        probe.arm_download(client, 23, MOVE, EDGE, 1)
        store.lookup_delay = 900
        before = probe.now()
        self.publish(client, store, [move()])
        row = probe.export()["downloads"][0]
        self.assertEqual(row["sqlite_persisted_us"], before + 100)
        self.assertGreaterEqual(probe.now(), row["sqlite_persisted_us"] + 900)

    def test_download_paint_waits_for_published_row_and_matching_board(self):
        client, store, _, view, probe, clock, _ = self.fixture()
        probe.arm_download(client, 7, MOVE, EDGE, 1)
        view.draw(client.controller)
        probe.panel_submitted()
        self.assertNotIn("framebuffer_us", probe.export()["downloads"][0])
        self.publish(client, store, [move()])
        client.controller.game.reset()
        view.draw(client.controller)
        probe.panel_submitted()
        self.assertNotIn("framebuffer_us", probe.export()["downloads"][0])
        client.controller.game.claim(*EDGE)
        view.draw(client.controller)
        clock.advance(16)
        probe.panel_submitted()
        row = probe.export()["downloads"][0]
        self.assertEqual(row["sample_id"], 7)
        self.assertEqual(row["outcome"], "accepted")
        self.assertGreater(row["panel_submitted_us"], row["framebuffer_us"])
        self.assertIsNone(probe.active)

    def test_download_arm_rejects_stale_identity_occupied_edge_and_unsynced_board(self):
        for setup in ("unsynced", "pending", "old_index", "occupied", "already_present"):
            with self.subTest(setup=setup):
                client, store, _, _, probe, _, _ = self.fixture()
                index = 1
                if setup == "unsynced":
                    client.sync.synced = False
                elif setup == "pending":
                    client.records.record["pending_move"] = {"id": OTHER_MOVE}
                elif setup == "old_index":
                    index = 0
                elif setup == "occupied":
                    client.controller.game.claim(*EDGE)
                    index = 2
                elif setup == "already_present":
                    store.history.append(move())
                with self.assertRaises(ValueError):
                    probe.arm_download(client, 8, MOVE, EDGE, index)
                self.assertEqual(probe.export()["downloads"], [])

    def test_refused_and_failed_moves_preserve_original_result_and_exception(self):
        client, _, session, _, probe, _, _ = self.fixture()
        client.controller.can_claim = False
        self.assertFalse(client.controller.on_claim(EDGE))
        self.assertEqual(probe.export()["local"], [])
        self.assertIsNone(probe.active)
        client.controller.can_claim = True
        session.fail = True
        with self.assertRaisesRegex(OSError, "durability barrier failed"):
            client.controller.on_claim(EDGE)
        row = probe.export()["local"][0]
        self.assertEqual(row["outcome"], "failed")
        self.assertNotIn("pending_saved_us", row)
        self.assertIsNone(client.records.record["pending_move"])

    def test_rejected_move_has_feedback_but_no_successful_upload_timestamp(self):
        client, _, _, view, probe, _, _ = self.fixture()
        client.controller.on_claim(EDGE)
        view.draw(client.controller)
        probe.panel_submitted()
        record = client.records.record
        client.records.save(dict(record, pending_move=None))
        client.sync._after_upload(accepted=False)
        client._apply_room()
        view.draw(client.controller)
        probe.panel_submitted()
        row = probe.export()["local"][0]
        self.assertEqual(row["outcome"], "rejected")
        self.assertIn("framebuffer_us", row)
        self.assertNotIn("rpc_done_us", row)
        self.assertNotIn("sqlite_persisted_us", row)
        self.assertIsNone(probe.active)

    def test_capacity_bounds_records_and_keeps_unmeasured_move_behavior(self):
        client, _, _, view, probe, _, _ = self.fixture(capacity=1)
        client.controller.on_claim(EDGE)
        view.draw(client.controller)
        probe.panel_submitted()
        client.records.save(dict(client.records.record, pending_move=None))
        client.sync._after_upload(accepted=False)
        probe.panel_submitted()
        client.sync.synced, client.sync.phase = True, "stream"
        client._apply_room()
        self.assertTrue(client.controller.on_claim(("h", 0, 1)))
        self.assertEqual(len(probe.export()["local"]), 1)
        self.assertEqual(probe.export()["dropped"], 1)

    def test_export_excludes_private_identifiers_tokens_and_preserves_incomplete_sample(self):
        client, _, _, _, probe, _, _ = self.fixture()
        client.controller.on_claim(EDGE)
        exported = probe.export()
        self.assertTrue(exported["active"])
        self.assertEqual(exported["local"][0]["outcome"], "incomplete")
        for secret in (MOVE, ROOM, USER, DEVICE, "secret-access", "secret-refresh", "_move_id", "_edge"):
            self.assertNotIn(secret, repr(exported))
        exported["local"][0]["outcome"] = "changed externally"
        self.assertEqual(probe.export()["local"][0]["outcome"], "incomplete")

    def test_detach_restores_methods_and_controller_callback_idempotently(self):
        client, store, _, view, probe, _, detach = self.fixture()
        self.assertIn("_queue_move", client.__dict__)
        self.assertIn("control_lines", store.__dict__)
        detach()
        detach()
        self.assertNotIn("_queue_move", client.__dict__)
        self.assertNotIn("control_lines", store.__dict__)
        self.assertNotIn("draw", view.__dict__)
        self.assertEqual(client.controller.on_claim, client._queue_move)
        self.assertTrue(client.controller.on_claim(EDGE))
        self.assertEqual(probe.export()["local"], [])

    def test_timer_rollover_accumulates_valid_ticks_diff_and_rejects_backwards_clock(self):
        values = iter((250, 3, 10, 240))
        probe = Probe(1, lambda: next(values), lambda a, b: ((a - b + 128) % 256) - 128)
        self.assertEqual(probe.now(), 9)
        self.assertEqual(probe.now(), 16)
        with self.assertRaisesRegex(ValueError, "backwards"):
            probe.now()

    def test_existing_pending_move_cannot_be_mistaken_for_new_local_write(self):
        client, _, _, view, _, _, detach = self.fixture()
        detach()
        client.records.record["pending_move"] = {"id": MOVE}
        with self.assertRaisesRegex(ValueError, "existing pending"):
            attach(client, view)


if __name__ == "__main__":
    unittest.main()
