"""Exercise the actual pinned core, bounded framing, and supervisor recovery."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/dots_boxes"))
from dots_http import PENDING, Response, HTTPS, READ_BYTES, STREAM_READ_BYTES
from dots_codec import decode_edge, encode_edge, encode_uuid
from dots_session import fresh_session, SessionFile
from dots_storage import Store, MAX_SYNC_BATCH_RECORDS
from dots_stream import StreamLines, MAX_FRAGMENT, MAX_LINE, MAX_METADATA, MAX_DEPTH
from dots_sync import Sync, MAX_BATCH_DELAY_MS, MAX_STREAM_READS, MAX_STREAM_RECORDS
from game import Game
from native_fixture import HostCore, LIBRARY

ROOM = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
DEVICE = "33333333-3333-4333-8333-333333333333"
TARGET = "9007199254740994"


class Records:
    def __init__(self):
        self.record = dict(fresh_session(DEVICE), room_id=ROOM, user_id=USER, seat=1,
                           access_token="saved-access-token", refresh_token="saved-refresh-token",
                           expires_at=10**12)

    def save(self, value):
        self.record = value


class JSONReply:
    def __init__(self, result):
        self.result = result
        self.closed = False

    def start(self, body):
        self.body = body

    def step(self):
        return self.result

    def close(self):
        self.closed = True


class FakeStore:
    def __init__(self):
        self.calls = []
        self.batches = []
        self.counter = 0
        self.published_moves = set()

    def retire_subscriptions(self):
        self.calls.append(("retire_subscriptions", None))

    def start_sync(self, stream, options, clear_target=False):
        self.retire_subscriptions()
        self.control("subscriptions", {"subscribe": {"stream": stream}})
        result = self.control("start", options)
        if clear_target:
            self.control("target_checkpoint_request_id", "0")
        return result

    def seed_checkpoint(self, accepted):
        self.counter = int(accepted)
        self.control("seed_checkpoint_request_id", accepted)
        return str(self.control("next_checkpoint_request_id"))

    def control(self, command, payload=None):
        self.calls.append((command, payload))
        if command == "start":
            return [{"EstablishSyncStream": {"request": {}, "checkpoint_request": {
                "client_id": DEVICE, "checkpoint_request_id": "1"}}}]
        if command == "line_text" and "checkpoint_complete" in payload:
            return [{"DidCompleteSync": {"applied_checkpoint_request_id": TARGET}}]
        if command == "next_checkpoint_request_id":
            self.counter += 1
            return self.counter
        return []

    def control_batch(self, lines):
        self.batches.append(list(lines))
        return []

    def control_lines(self, lines, parsed=None):
        self.batches.append(list(lines))
        decoded = [json.loads(line) for line in lines] if parsed is None else parsed
        return [{"instructions": self.control("line_text", line),
                 "full_checkpoint": "checkpoint_complete" in record}
                for line, record in zip(lines, decoded)]

    def has_local_writes(self):
        return False

    def has_move(self, move_id, room_id):
        return (move_id, room_id) in self.published_moves


class Stream:
    def __init__(self, *args, **_kwargs):
        self.body = args[1]
        self.status = None
        self.parts = []
        self.closed = False

    def step(self):
        self.status = 200
        return self.parts.pop(0) if self.parts else PENDING

    def close(self):
        self.closed = True


def make_sync(store=None, records=None, replies=None):
    replies = list([
        {"data": {"checkpoint_request_id": "9007199254740993"}},
        {"data": {"checkpoint_request_id": TARGET}},
    ] if replies is None else replies)

    def factory(*_args, **_kwargs):
        return JSONReply(replies.pop(0))

    return Sync(store or FakeStore(), records or Records(), "https://sync.example",
                "https://auth.example/token", "https://auth.example/move", "a" * 24, (),
                http_factory=Stream, json_factory=factory)


def start(sync):
    for _ in range(10):
        sync.step(1)
        if sync.phase == "stream":
            break
    assert sync.phase == "stream", (sync.phase, sync.last_error)


def wire(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def operation(index, table="moves", body=None, compact=True):
    key = ROOM if table == "rooms" else "%08x-4444-4444-8444-444444444444" % index
    orientation, row, col = decode_edge((index - 1) % 49)
    body = body or {"id": key, "room_id": ROOM, "seat": 1, "orientation": orientation,
                    "row": row, "col": col, "move_index": index}
    key = body.get("id", key)
    if compact:
        key = encode_uuid(key)
        if table == "rooms" and "status" in body:
            body = {"s": ("waiting", "playing", "finished", "cancelled").index(body["status"]),
                    "p": body["current_seat"], "a": body["score_p1"], "b": body["score_p2"],
                    "w": body["winner"], "n": body["move_count"]}
        elif table == "moves" and "room_id" in body:
            body = {"r": encode_uuid(body["room_id"]),
                    "e": encode_edge(body["orientation"], body["row"], body["col"]),
                    "n": body["move_index"]}
    return {"op_id": str(index), "op": "PUT", "object_type": table,
            "object_id": key, "checksum": index, "data": json.dumps(body)}


def checkpoint_records(bucket, operations):
    buckets = [{"bucket": bucket, "checksum": sum(op["checksum"] for op in operations),
                "priority": 3, "count": len(operations)}] if operations else []
    records = [{"checkpoint": {"last_op_id": operations[-1]["op_id"] if operations else "0", "buckets": buckets}}]
    if operations:
        records.append({"data": {"bucket": bucket, "data": operations}})
    return records + [{"checkpoint_complete": {}}]


class SupervisorTests(unittest.TestCase):
    def test_download_continues_across_two_available_fragments_without_frame_delay(self):
        sync = make_sync()
        start(sync)
        data = wire({"token_expires_in": "x" * (MAX_FRAGMENT + 10)})
        sync.stream.parts = [data[:MAX_FRAGMENT], data[MAX_FRAGMENT:]]
        with patch.object(sync.stream, "step", wraps=sync.stream.step) as reads:
            sync.step(1)
        self.assertEqual(reads.call_count, 2)
        self.assertEqual(len(sync.batch), 1)
        self.assertEqual(sync.parsed_batch, [json.loads(data)])
        self.assertEqual(sync.lines.offset, len(sync.lines.pending))

    def test_download_has_fixed_transport_and_record_budgets_per_update(self):
        sync = make_sync()
        start(sync)
        operations = [operation(index) for index in range(1, 50)]
        data = b"".join(wire(record) for record in checkpoint_records("room", operations))
        sync.stream.parts = [data[offset:offset + MAX_FRAGMENT] for offset in range(0, len(data), MAX_FRAGMENT)]
        emitted = steps = 0
        while not sync.synced and steps < 30:
            with patch.object(sync.stream, "step", wraps=sync.stream.step) as reads:
                sync.step(1)
            self.assertLessEqual(reads.call_count, MAX_STREAM_READS)
            current = sum(len(batch) for batch in sync.store.batches) + len(sync.batch)
            self.assertLessEqual(current - emitted, MAX_STREAM_RECORDS)
            emitted = current
            steps += 1
        self.assertTrue(sync.synced)
        self.assertEqual(emitted, 51)
        self.assertEqual(steps, 9)
        self.assertTrue(all(len(batch) <= MAX_SYNC_BATCH_RECORDS for batch in sync.store.batches))

    def test_download_budget_includes_the_batch_carried_from_an_earlier_update(self):
        sync = make_sync()
        start(sync)
        first = [{"token_expires_in": value} for value in range(MAX_STREAM_RECORDS - 1)]
        sync.stream.parts = [b"".join(wire(record) for record in first)]
        sync.step(1)
        self.assertEqual(len(sync.batch), MAX_STREAM_RECORDS - 1)
        self.assertEqual(sync.store.batches, [])
        following = [{"token_expires_in": value} for value in range(MAX_STREAM_RECORDS - 1)]
        following.append({"checkpoint_complete": {}})
        sync.stream.parts = [b"".join(wire(record) for record in following)]
        sync.step(1)
        self.assertEqual([len(batch) for batch in sync.store.batches], [MAX_STREAM_RECORDS])
        self.assertFalse(sync.synced)
        self.assertEqual(sync.batch, [])
        sync.step(1)
        self.assertEqual([len(batch) for batch in sync.store.batches], [MAX_STREAM_RECORDS, MAX_STREAM_RECORDS - 1])
        self.assertTrue(sync.synced)
        actual = [json.loads(line) for batch in sync.store.batches for line in batch]
        self.assertEqual(actual, first + following)

    def test_accepted_move_reconnects_without_uploading_again_in_same_process(self):
        sync = make_sync(replies=[{"accepted": True}])
        start(sync)
        sync.connected = sync.synced = True
        move = {"id": DEVICE, "orientation": "h", "row": 0, "col": 0}
        sync.records.save(dict(sync.records.record, pending_move=move))
        sync.step(1)
        sync.step(1)
        self.assertEqual(sync.phase, "stream")
        sync._retry()
        with patch.object(sync, "_post", side_effect=AssertionError("duplicate same-process upload")), \
                patch.object(sync, "_checkpoint_request", side_effect=AssertionError("accepted checkpoint HTTP")):
            sync.step(2000)
            start(sync)
            sync.stream.parts = [wire({"checkpoint_complete": {}})]
            sync.step(1)
            self.assertEqual(sync.records.record["pending_move"], move)
            self.assertFalse(sync.synced)
            sync.store.published_moves.add((DEVICE, ROOM))
            sync.stream.parts = [wire({"checkpoint_complete": {}})]
            sync.step(1)
        self.assertTrue(sync.synced)
        self.assertIsNone(sync.records.record["pending_move"])

    def test_metadata_tail_waits_for_bound_instead_of_flushing_on_would_block(self):
        sync = make_sync()
        start(sync)
        sync.stream.parts = [wire({"token_expires_in": 300})]
        sync.step(1)
        sync.step(1)
        sync.step(MAX_BATCH_DELAY_MS - 2)
        self.assertEqual(sync.store.batches, [])
        sync.step(1)
        self.assertEqual(len(sync.store.batches), 1)

    def test_buffered_metadata_and_completion_commit_together_even_on_slow_frames(self):
        sync = make_sync()
        start(sync)
        records = [{"sync_stream_started": {}}, {"checkpoint": {"last_op_id": "0", "buckets": []}},
                   {"token_expires_in": 300}, {"checkpoint_complete": {}}]
        data = b"".join(wire(record) for record in records)
        self.assertLessEqual(len(data), 512)
        sync.stream.parts = [data]
        sync.step(120)
        self.assertTrue(sync.synced)
        self.assertEqual(len(sync.store.batches), 1)
        self.assertEqual(len(sync.store.batches[0]), 4)

    def test_fragmented_checkpoint_does_not_commit_between_tls_reads(self):
        sync = make_sync()
        start(sync)
        records = [{"token_expires_in": 300}, {"checkpoint_diff": {"last_op_id": "1"}},
                   {"data": {"bucket": "room", "data": [operation(1)]}},
                   {"checkpoint_complete": {}}]
        # Slow frames still fit within the bounded idle window. No transaction
        # is held while the next TCP fragment is unavailable.
        for record in records[:-1]:
            sync.stream.parts = [wire(record)]
            sync.step(220)
            sync.step(1)
            self.assertEqual(sync.store.batches, [])
        sync.stream.parts = [wire(records[-1])]
        sync.step(220)
        self.assertTrue(sync.synced)
        self.assertEqual(len(sync.store.batches), 1)
        self.assertEqual([json.loads(line) for line in sync.store.batches[0]], records)

    def test_available_completion_is_read_before_expired_idle_flush(self):
        sync = make_sync()
        start(sync)
        sync.stream.parts = [wire({"checkpoint": {"last_op_id": "0", "buckets": []}})]
        sync.step(1)
        sync.stream.parts = [wire({"checkpoint_complete": {}})]
        sync.step(MAX_BATCH_DELAY_MS + 100)
        self.assertTrue(sync.synced)
        self.assertEqual(len(sync.store.batches), 1)
        self.assertEqual(len(sync.store.batches[0]), 2)

    def test_partial_completion_is_a_flush_boundary_but_cannot_mark_ready(self):
        sync = make_sync()
        start(sync)
        data = wire({"checkpoint": {"last_op_id": "0", "buckets": []}})
        data += wire({"partial_checkpoint_complete": {"priority": 3}})
        data += wire({"checkpoint_complete": {}})
        sync.stream.parts = [data]
        sync.step(1)
        self.assertTrue(sync.synced)
        self.assertEqual([len(batch) for batch in sync.store.batches], [2, 1])

    def test_delayed_checkpoint_only_errors_when_sql_outbox_is_nonempty(self):
        sync = make_sync()
        sync._instructions([{"CheckpointDelayedDueToLocalData": {}}])
        self.assertEqual(sync.status, "SYNCING")
        with patch.object(sync.store, "has_local_writes", return_value=True), \
                self.assertRaisesRegex(ValueError, "Unexpected local SQL writes"):
            sync._instructions([{"CheckpointDelayedDueToLocalData": {}}])

    def test_read_startup_requires_full_checkpoint_and_no_checkpoint_http(self):
        sync = make_sync(replies=[])
        with patch.object(sync, "_checkpoint_request", side_effect=AssertionError("startup HTTP checkpoint")):
            start(sync)
            sync._instructions([{"DidCompleteSync": {}}])
            self.assertFalse(sync.synced)
            sync.stream.parts = [wire({"checkpoint_complete": {"last_op_id": "0"}})]
            sync.step()
        self.assertTrue(sync.synced)
        self.assertFalse(sync.checkpoint_seeded)

    def test_reboot_after_http_accept_replays_saved_move_and_waits_for_fresh_target(self):
        records = Records()
        sync = make_sync(records=records, replies=[{"accepted": True}])
        start(sync)
        sync.connected = sync.synced = True
        move = {"id": DEVICE, "orientation": "h", "row": 0, "col": 0}
        records.save(dict(records.record, pending_move=move))
        sync.step()
        sync.step()
        self.assertEqual(sync.phase, "stream")
        self.assertEqual(records.record["pending_move"], move)
        sync.close()
        restored = make_sync(records=records, replies=[
            {"accepted": True},
        ])
        with patch.object(restored, "_post", wraps=restored._post) as post:
            start(restored)
        self.assertEqual(post.call_args.args[1]["move_id"], DEVICE)
        self.assertFalse(restored.synced)
        self.assertEqual(records.record["pending_move"], move)
        restored.store.published_moves.add((DEVICE, ROOM))
        restored.stream.parts = [wire({"checkpoint_complete": {"last_op_id": "1"}})]
        restored.step()
        self.assertTrue(restored.synced)
        self.assertIsNone(records.record["pending_move"])

    def test_second_tls_memory_failure_falls_back_without_losing_pending_uuid(self):
        sync = make_sync()
        start(sync)
        sync.connected = sync.synced = True
        stream = sync.stream
        sync.records.record["pending_move"] = {"id": DEVICE, "orientation": "h", "row": 0, "col": 0}
        sync.step()
        with patch.object(sync, "_post", side_effect=MemoryError):
            sync.step()
        self.assertFalse(sync.live_upload)
        self.assertTrue(stream.closed)
        self.assertEqual((sync.phase, sync.status), ("token", "OFFLINE"))
        self.assertEqual(sync.records.record["pending_move"]["id"], DEVICE)
        sync.step(2000)
        self.assertEqual(sync.phase, "move")
        self.assertIsNone(sync.stream)

    def test_rejected_move_also_waits_for_covering_server_state(self):
        sync = make_sync(replies=[
            {"message": "not your turn", "http_status": 400},
            {"data": {"checkpoint_request_id": TARGET}},
            {"data": {"checkpoint_request_id": "9007199254740995"}},
        ])
        start(sync)
        sync.connected = sync.synced = True
        sync.records.record["pending_move"] = {"id": DEVICE, "orientation": "h", "row": 0, "col": 0}
        sync.step()
        sync.step()
        self.assertEqual(sync.phase, "move_seed")
        self.assertFalse(sync.synced)
        self.assertIn("not your turn", sync.notice)
        self.assertIsNone(sync.records.record["pending_move"])

    def test_move_reuses_stream_and_waits_for_covering_checkpoint(self):
        sync = make_sync(replies=[{"accepted": True}])
        start(sync)
        sync.stream.step()
        sync.connected = sync.synced = True
        stream = sync.stream
        sync.records.record["pending_move"] = {"id": DEVICE, "orientation": "h", "row": 0, "col": 0}
        sync.step()
        self.assertEqual(sync.phase, "move")
        self.assertFalse(sync.synced)
        sync.step()
        self.assertEqual(sync.records.record["pending_move"]["id"], DEVICE)
        self.assertEqual(sync.phase, "stream")
        self.assertIs(sync.stream, stream)
        self.assertFalse(stream.closed)
        self.assertFalse(sync.synced)
        self.assertIsNone(sync.target)
        self.assertEqual(sum(command == "start" for command, _ in sync.store.calls), 1)
        self.assertFalse(any(command == "stop" for command, _ in sync.store.calls))
        sync._instructions([{"DidCompleteSync": {}}], full_checkpoint=True)
        self.assertFalse(sync.synced)
        sync.store.published_moves.add((DEVICE, USER))
        sync._instructions([{"DidCompleteSync": {}}], full_checkpoint=True)
        self.assertFalse(sync.synced)
        sync.store.published_moves.add((DEVICE, ROOM))
        sync._instructions([{"DidCompleteSync": {}}])
        self.assertFalse(sync.synced)
        sync._instructions([{"DidCompleteSync": {}}], full_checkpoint=True)
        self.assertTrue(sync.synced)
        self.assertIsNone(sync.records.record["pending_move"])

    def test_batches_preserve_raw_operations_and_commit_before_checkpoint(self):
        sync = make_sync()
        start(sync)
        operations = [operation(i) for i in range(1, 15)]
        data = wire({"data": {"bucket": "room", "data": operations}})
        data += wire({"checkpoint_complete": {"last_op_id": "14"}})
        sync.stream.parts = [data[i:i + 512] for i in range(0, len(data), 512)]
        for _ in range(200):
            sync.step(1)
            if sync.synced:
                break
        self.assertTrue(sync.synced)
        self.assertEqual(sync.generation, 1)
        self.assertTrue(all(len(batch) <= MAX_SYNC_BATCH_RECORDS for batch in sync.store.batches))
        actual = [json.loads(line)["data"]["data"][0]
                  for batch in sync.store.batches for line in batch if "data" in json.loads(line)]
        self.assertEqual(actual, operations)
        self.assertEqual([len(batch) for batch in sync.store.batches], [6, 6, 3])
        self.assertEqual(sync.batch, [])

    def test_small_batch_flushes_when_network_is_idle(self):
        sync = make_sync()
        start(sync)
        sync.stream.parts = [wire({"data": {"bucket": "b", "data": [operation(1)]}})]
        for _ in range(MAX_BATCH_DELAY_MS // 10 + 2):
            sync.step(10)
        self.assertEqual(len(sync.store.batches), 1)

    def test_rejected_checkpoint_token_forces_refresh_instead_of_retrying_same_token(self):
        sync = make_sync(replies=[{"error": "unauthorized", "http_status": 401},
                                 {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}])
        start(sync)
        sync.phase = "move_seed"
        sync.step(1)
        self.assertEqual(sync.status, "OFFLINE")
        self.assertIsNone(sync.records.record["access_token"])
        sync.step(2000)
        self.assertEqual(sync.records.record["access_token"], "new-access")
        self.assertEqual(sync.phase, "start")

    def test_missing_stream_is_visible_and_stops_retry_loop(self):
        sync = make_sync()
        with patch.object(sync.store, "control", side_effect=RuntimeError("SQLite operation failed")):
            sync.phase = "start"
            sync.step()
        self.assertEqual((sync.phase, sync.status), ("blocked", "SYNC ERROR"))
        self.assertIn("SQLite", sync.last_error)
        sync = make_sync()
        with self.assertRaisesRegex(ValueError, "update sync configuration"):
            sync._instructions([{"LogLine": {"severity": "WARNING", "line":
                "Subscription to stream badge_game_v2 (without parameters) could not be resolved: missing stream"}}])

    def test_protocol_failure_releases_stream_and_uncommitted_batch(self):
        sync = make_sync()
        start(sync)
        stream = sync.stream
        stream.parts = [b'{"checkpoint":[]}\n']
        with patch.object(sync.store, "control_lines", side_effect=ValueError("bad checkpoint")):
            sync.step()
            sync.step(MAX_BATCH_DELAY_MS)
        self.assertTrue(stream.closed)
        self.assertEqual(sync.phase, "blocked")
        self.assertFalse(sync.synced)
        self.assertEqual(sync.batch, [])

    def test_retry_and_close_never_discard_durable_pending_move(self):
        records = Records()
        records.record["pending_move"] = {"id": DEVICE, "orientation": "h", "row": 0, "col": 0}
        sync = make_sync(records=records, replies=[{"error": "unavailable", "http_status": 503}])
        sync.step()
        sync.step()
        self.assertEqual(sync.status, "OFFLINE")
        sync.close()
        self.assertEqual(records.record["pending_move"]["id"], DEVICE)


@unittest.skipUnless(LIBRARY.exists(), "Pinned badgegotchi host qualifier is not available")
class RealCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.native = HostCore()
        self.store = Store(native=self.native, root=self.temp.name + "/dots")
        self.store.open()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def deliver(self, sync, records, client=None):
        stream = sync.stream
        data = b"".join(wire(record) for record in records)
        stream.parts = [data[i:i + 512] for i in range(0, len(data), 512)]
        for _ in range(100):
            if client is None:
                sync.step(5)
            else:
                client.step([], 5)
            self.assertEqual(sync.phase, "stream", sync.last_error)
            if not stream.parts and sync.lines.offset == len(sync.lines.pending) and not sync.batch:
                return
        self.fail("Native stream did not drain")

    def test_accepted_uuid_replays_after_reopen_and_only_canonical_checkpoint_clears_file(self):
        from dots_client import Records as SavedRecords
        move = {"id": DEVICE, "orientation": "h", "row": 0, "col": 0}
        session = SessionFile(self.store.root)
        session.save(dict(Records().record, pending_move=move))
        sync = make_sync(self.store, records=SavedRecords(session, session.load(self.store.new_uuid)),
                         replies=[{"ok": True}])
        with patch.object(sync, "_post", wraps=sync._post) as first_upload, \
                patch.object(sync, "_checkpoint_request", side_effect=AssertionError("initial replay checkpoint HTTP")):
            start(sync)
        self.assertEqual(first_upload.call_count, 1)
        self.assertEqual(first_upload.call_args.args[1]["move_id"], DEVICE)
        self.assertEqual(session.load(self.store.new_uuid)["pending_move"], move)
        # Legacy versions could leave an unobserved checkpoint target behind.
        self.store.control("target_checkpoint_request_id", "999")
        sync.close()
        self.store.close()
        self.store.open()
        restored = make_sync(self.store, records=SavedRecords(session, session.load(self.store.new_uuid)),
                             replies=[{"ok": True, "duplicate": True}])
        with patch.object(restored, "_post", wraps=restored._post) as replay, \
                patch.object(restored, "_checkpoint_request", side_effect=AssertionError("resumed replay checkpoint HTTP")):
            start(restored)
            self.assertEqual(replay.call_count, 1)
            self.assertEqual(replay.call_args.args[1]["move_id"], DEVICE)
            self.assertEqual(self.native.exec("SELECT count(*) FROM ps_kv WHERE key='target_checkpoint_request_id'"), [["0"]])
            self.deliver(restored, checkpoint_records("empty", []))
            self.assertEqual(session.load(self.store.new_uuid)["pending_move"], move)
            self.assertFalse(restored.synced)
            body = {"id": DEVICE, "room_id": ROOM, "seat": 1, "orientation": "h", "row": 0, "col": 0, "move_index": 1}
            accepted = dict(operation(1, body=body), object_id=encode_uuid(DEVICE))
            room = operation(2, "rooms", {"id": ROOM, "status": "playing", "cols": 5, "rows": 4,
                "current_seat": 2, "score_p1": 0, "score_p2": 0, "winner": None, "move_count": 1})
            self.deliver(restored, checkpoint_records("paired", [accepted, room]))
        self.assertTrue(restored.synced)
        self.assertIsNone(session.load(self.store.new_uuid)["pending_move"])
        self.assertTrue(self.store.has_move(DEVICE, ROOM))
        self.assertEqual(self.store.room(ROOM)["move_count"], 1)
        self.assertFalse(self.store.has_local_writes())
        restored.close()

    def test_confirmed_move_refreshes_client_board_once_and_keeps_idle_frames_cached(self):
        from dots_client import Client
        self.store.bind_account(USER)
        session = SessionFile(self.store.root)
        session.save(Records().record)
        client = Client(self.store, session)
        client.sync = make_sync(self.store, records=client.records, replies=[{"ok": True}])
        sync = client.sync
        start(sync)
        initial = {"id": ROOM, "status": "playing", "cols": 5, "rows": 4,
                   "current_seat": 1, "score_p1": 0, "score_p2": 0, "winner": None, "move_count": 0}
        self.deliver(sync, checkpoint_records("initial", [operation(1, "rooms", initial)]), client)
        self.assertTrue(client.controller.can_claim)
        self.assertTrue(client._queue_move(("h", 0, 0)))
        pending_id = client.records.record["pending_move"]["id"]
        self.assertEqual(client.controller.pending_edge, ("h", 0, 0))
        self.assertEqual(client.controller.pending_player, 1)
        self.assertFalse(client.controller.can_claim)
        client.step([], 5)
        client.step([], 5)
        self.assertEqual(sync.uploaded_move_id, pending_id)
        self.assertEqual(client.controller.pending_edge, ("h", 0, 0))
        self.assertEqual(client.controller.game.edge_owner("h", 0, 0), 0)
        self.assertFalse(client.controller.can_claim)
        accepted = dict(operation(2, body={"id": pending_id, "room_id": ROOM, "seat": 1,
            "orientation": "h", "row": 0, "col": 0, "move_index": 1}), object_id=encode_uuid(pending_id))
        room = operation(3, "rooms", dict(initial, current_seat=2, move_count=1))
        with patch.object(self.store, "room", wraps=self.store.room) as room_reads, \
                patch.object(self.store, "moves", wraps=self.store.moves) as move_reads:
            self.deliver(sync, checkpoint_records("current", [accepted, room]), client)
            self.assertEqual((room_reads.call_count, move_reads.call_count), (1, 1))
            for _ in range(180):
                client.step([], 16)
            self.assertEqual((room_reads.call_count, move_reads.call_count), (1, 1))
        self.assertIsNone(client.records.record["pending_move"])
        self.assertIsNone(client.controller.pending_edge)
        self.assertEqual(client.controller.game.moves, 1)
        self.assertEqual(client.controller.game.edge_owner("h", 0, 0), 1)
        self.assertEqual(client.controller.game.current_player, 2)
        self.assertFalse(client.controller.can_claim)
        client.close()

    def test_rejected_move_removes_local_edge_before_covering_checkpoint(self):
        from dots_client import Client
        self.store.bind_account(USER)
        session = SessionFile(self.store.root)
        session.save(Records().record)
        client = Client(self.store, session)
        client.sync = make_sync(self.store, records=client.records, replies=[
            {"http_status": 409, "message": "Move rejected"},
        ])
        sync = client.sync
        start(sync)
        initial = {"id": ROOM, "status": "playing", "cols": 5, "rows": 4,
                   "current_seat": 1, "score_p1": 0, "score_p2": 0, "winner": None, "move_count": 0}
        self.deliver(sync, checkpoint_records("initial", [operation(1, "rooms", initial)]), client)
        generation = sync.generation
        self.assertTrue(client._queue_move(("h", 0, 0)))
        self.assertEqual(client.controller.pending_edge, ("h", 0, 0))
        client.step([], 5)
        client.step([], 5)
        self.assertEqual(sync.generation, generation)
        self.assertIsNone(client.records.record["pending_move"])
        self.assertIsNone(client.controller.pending_edge)
        self.assertEqual(client.controller.game.edge_owner("h", 0, 0), 0)
        self.assertEqual(client.controller.status_line, "Move rejected")
        self.assertFalse(client.controller.can_claim)
        client.close()

    def cached_room(self):
        self.store.bind_account(USER)
        sync = make_sync(self.store)
        start(sync)
        body = {"id": ROOM, "status": "finished", "cols": 5, "rows": 4, "current_seat": 1,
                "score_p1": 0, "score_p2": 0, "winner": None, "move_count": 0}
        data = wire({"checkpoint": {"last_op_id": "1", "write_checkpoint": TARGET,
            "buckets": [{"bucket": "room", "checksum": 1, "priority": 3, "count": 1}]}})
        data += wire({"data": {"bucket": "room", "data": [operation(1, "rooms", body)]}})
        data += wire({"checkpoint_complete": {"last_op_id": "1"}})
        sync.stream.parts = [data[i:i + 512] for i in range(0, len(data), 512)]
        for _ in range(100):
            sync.step(1)
            if sync.synced:
                break
        self.assertTrue(sync.synced, sync.last_error)
        sync.close()
        return self.store.room(ROOM)

    def test_saved_replacement_session_clears_old_account_cache_on_startup(self):
        from dots_client import Client
        self.cached_room()
        replacement = dict(Records().record, user_id="44444444-4444-4444-8444-444444444444",
                           room_id="55555555-5555-4555-8555-555555555555")
        session = SessionFile(self.store.root)
        session.save(replacement)
        client_id = self.native.exec("SELECT value FROM ps_kv WHERE key='client_id'")
        # Simulate reset immediately after session save, before SQL reconciliation.
        self.store.close()
        self.store.open()
        client = Client(self.store, session)
        try:
            self.assertIsNone(self.store.room(ROOM))
            for table in ("ps_oplog", "ps_buckets", "ps_stream_subscriptions", "ps_crud"):
                self.assertEqual(self.native.exec("SELECT count(*) FROM " + table), [["0"]])
            self.assertEqual(self.native.exec("SELECT user_id FROM account WHERE id='self'"), [[replacement["user_id"]]])
            self.assertEqual(self.native.exec("SELECT value FROM ps_kv WHERE key='client_id'"), client_id)
            self.assertEqual(session.load(self.store.new_uuid), replacement)
            self.assertFalse(self.store.reconcile_session(replacement))
        finally:
            client.close()

    def test_interrupted_cache_switch_rolls_back_and_recovers_before_any_board_read(self):
        from dots_client import Client
        old_room = self.cached_room()
        replacement = dict(Records().record, user_id="44444444-4444-4444-8444-444444444444")
        session = SessionFile(self.store.root)
        session.save(replacement)
        original = self.store._exec

        def interrupted(sql):
            if sql.startswith("UPDATE account SET user_id="):
                raise OSError("simulated reset after native clear")
            return original(sql)

        with patch.object(self.store, "_exec", side_effect=interrupted), \
                patch.object(self.store, "room") as read, self.assertRaises(OSError):
            Client(self.store, session)
        read.assert_not_called()
        self.assertEqual(self.store.room(ROOM), old_room)
        self.assertEqual(self.native.exec("SELECT user_id FROM account WHERE id='self'"), [[USER]])
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_buckets"), [["1"]])
        self.store.close()
        self.store.open()
        client = Client(self.store, session)
        try:
            self.assertIsNone(self.store.room(ROOM))
            self.assertEqual(client.records.record, replacement)
            self.assertFalse(self.store.bind_account(replacement["user_id"]))
        finally:
            client.close()

    def test_pending_move_blocks_cross_account_clear_and_survives_same_account_reopen(self):
        old_room = self.cached_room()
        pending = dict(Records().record, pending_move={"id": DEVICE, "orientation": "h", "row": 0, "col": 0})
        session = SessionFile(self.store.root)
        session.save(pending)
        self.store.close()
        self.store.open()
        self.assertFalse(self.store.reconcile_session(pending))
        with self.assertRaisesRegex(ValueError, "Saved move belongs to another account"):
            self.store.reconcile_session(dict(pending, user_id="44444444-4444-4444-8444-444444444444"))
        self.assertEqual(self.store.room(ROOM), old_room)
        self.assertFalse(self.store.bind_account(USER))
        self.assertEqual(session.load(self.store.new_uuid), pending)

    def test_raw_tables_reopen_sync_updates_and_removes_publish_only_at_checkpoint(self):
        self.store.bind_account(USER)
        sync = make_sync(self.store)
        start(sync)
        initial_room = {"id": ROOM, "status": "playing", "cols": 5, "rows": 4,
                        "current_seat": 2, "score_p1": 0, "score_p2": 0,
                        "winner": None, "move_count": 1}
        move_body = {"id": DEVICE, "room_id": ROOM, "seat": 1, "orientation": "h",
                     "row": 0, "col": 0, "move_index": 1}
        self.deliver(sync, checkpoint_records("raw-room", [
            operation(1, body=move_body), operation(2, "rooms", initial_room)]))
        self.assertTrue(sync.synced, sync.last_error)
        sync.close()
        client_id = self.native.exec("SELECT value FROM ps_kv WHERE key='client_id'")
        self.store.close()
        self.store.open()
        self.assertEqual(self.native.exec("PRAGMA user_version"), [["5"]])
        self.assertEqual(self.native.exec(
            "SELECT name,type FROM sqlite_master WHERE name IN ('rooms','moves') ORDER BY name"),
            [["moves", "table"], ["rooms", "table"]])
        self.assertEqual(self.native.exec(
            "SELECT name FROM sqlite_master WHERE name IN ('ps_data__rooms','ps_data__moves')"), [])
        self.assertTrue(self.store.has_move(DEVICE, ROOM))
        sync = make_sync(self.store)
        start(sync)

        # Continue the same bucket after reopening. The core must have the raw
        # mappings on this connection to publish both updates and removals.
        def stage(operations, checksum, count):
            self.store.control_lines([json.dumps(record) for record in [
                {"checkpoint": {"last_op_id": operations[-1]["op_id"], "buckets": [
                    {"bucket": "raw-room", "checksum": checksum, "priority": 3, "count": count}]}},
                {"data": {"bucket": "raw-room", "data": operations}},
            ]])

        update = operation(3, body=dict(move_body, orientation="v"))
        room = operation(4, "rooms", dict(initial_room, current_seat=1))
        stage([update, room], checksum=10, count=4)
        self.assertEqual(self.store.moves(ROOM)[0]["orientation"], "h")
        self.assertEqual(self.store.room(ROOM)["current_seat"], 2)
        self.deliver(sync, [{"checkpoint_complete": {}}])
        self.assertTrue(sync.synced, sync.last_error)
        self.assertEqual(self.store.moves(ROOM), [
            {"orientation": "v", "row": 0, "col": 0, "move_index": 1}])
        self.assertEqual(self.store.room(ROOM)["current_seat"], 1)
        remove = {"op_id": "5", "op": "REMOVE", "object_type": "moves",
                  "object_id": encode_uuid(DEVICE), "checksum": 5}
        room = operation(6, "rooms", dict(initial_room, current_seat=1, move_count=0))
        stage([remove, room], checksum=21, count=6)
        self.assertTrue(self.store.has_move(DEVICE, ROOM))
        self.assertEqual(self.store.room(ROOM)["move_count"], 1)
        self.deliver(sync, [{"checkpoint_complete": {}}])
        self.assertTrue(sync.synced, sync.last_error)
        self.assertFalse(self.store.has_move(DEVICE, ROOM))
        self.assertEqual(self.store.moves(ROOM), [])
        self.assertEqual(self.store.room(ROOM)["move_count"], 0)
        self.assertFalse(self.store.has_local_writes())
        self.assertEqual(self.native.exec("SELECT value FROM ps_kv WHERE key='client_id'"), client_id)
        self.assertFalse(self.store.bind_account(USER))
        sync.close()
        self.store.close()
        self.store.open()
        self.assertEqual(self.store.moves(ROOM), [])
        self.assertEqual(self.store.room(ROOM)["move_count"], 0)
        self.assertFalse(self.store.has_local_writes())

    def test_raw_account_switch_after_reopen_clears_both_tables_and_preserves_local_rows(self):
        self.store.bind_account(USER)
        self.store._exec("INSERT INTO account(id,user_id) VALUES('local-marker','%s')" % DEVICE)
        session = SessionFile(self.store.root)
        session.save(Records().record)
        sync = make_sync(self.store)
        start(sync)
        self.deliver(sync, checkpoint_records("old-account", [
            operation(1, body={"id": DEVICE, "room_id": ROOM, "seat": 1, "orientation": "h",
                               "row": 0, "col": 0, "move_index": 1}),
            operation(2, "rooms", {"id": ROOM, "status": "playing", "cols": 5, "rows": 4,
                "current_seat": 2, "score_p1": 0, "score_p2": 0, "winner": None, "move_count": 1}),
        ]))
        self.assertTrue(sync.synced, sync.last_error)
        sync.close()
        client_id = self.native.exec("SELECT value FROM ps_kv WHERE key='client_id'")
        replacement = dict(Records().record, user_id="44444444-4444-4444-8444-444444444444",
                           room_id="55555555-5555-4555-8555-555555555555")
        session.save(replacement)
        self.store.close()
        self.store.open()
        self.assertTrue(self.store.has_move(DEVICE, ROOM))
        self.assertTrue(self.store.reconcile_session(replacement))
        for table in ("rooms", "moves", "ps_oplog", "ps_buckets", "ps_stream_subscriptions", "ps_crud"):
            self.assertEqual(self.native.exec("SELECT count(*) FROM " + table), [["0"]])
        self.assertEqual(self.native.exec("SELECT user_id FROM account WHERE id='self'"), [[replacement["user_id"]]])
        self.assertEqual(self.native.exec("SELECT user_id FROM account WHERE id='local-marker'"), [[DEVICE]])
        self.assertEqual(self.native.exec("SELECT value FROM ps_kv WHERE key='client_id'"), client_id)
        self.assertEqual(session.load(self.store.new_uuid), replacement)
        self.assertFalse(self.store.has_local_writes())
        self.store.close()
        self.store.open()
        self.assertEqual(self.store.moves(ROOM), [])
        self.assertIsNone(self.store.room(ROOM))
        self.assertFalse(self.store.bind_account(replacement["user_id"]))
        self.assertFalse(self.store.has_local_writes())

    def test_real_request_uses_one_room_and_removes_all_persisted_ttls(self):
        for name, params in (("lobby", None), ("active_game", None),
                             ("badge_game", {"room_id": USER})):
            self.store.control("subscriptions", {"subscribe": {
                "stream": {"name": name, "params": params}, "ttl": 300}})
        sync = make_sync(self.store)
        start(sync)
        request = sync.stream.body
        self.assertEqual(request["parameters"], {})
        self.assertTrue(request["raw_data"])
        self.assertEqual(request["streams"], {"include_defaults": False, "subscriptions": [
            {"stream": "badge_game_v2", "parameters": {"room_id": ROOM}, "override_priority": None}]})
        sync.close()
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_stream_subscriptions WHERE ttl IS NOT NULL"), [["0"]])

    def test_startup_and_seed_each_use_one_full_transaction(self):
        self.store.control("start", {"checkpoint_mode": "requests", "parameters": {}})
        self.store.control("target_checkpoint_request_id", "42")
        self.store.control("stop")
        self.store.control("subscriptions", {"subscribe": {
            "stream": {"name": "old", "params": {"room_id": USER}}, "ttl": 300}})
        before = self.native.transactions
        sync = make_sync(self.store)
        start(sync)
        self.assertEqual(self.native.transactions - before, 1)
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_kv WHERE key='target_checkpoint_request_id'"), [["0"]])
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_stream_subscriptions WHERE ttl IS NOT NULL"), [["1"]])
        before = self.native.transactions
        self.assertEqual(self.store.seed_checkpoint("9007199254740993"), TARGET)
        self.assertEqual(self.native.transactions - before, 1)
        sync.close()

    def test_startup_commit_failure_rolls_back_all_subscription_changes(self):
        original = self.store._exec

        def failed_commit(sql):
            if sql == "COMMIT":
                raise OSError("simulated failed sync setup commit")
            return original(sql)

        stream = {"name": "badge_game_v2", "params": {"room_id": ROOM}}
        with patch.object(self.store, "_exec", side_effect=failed_commit), self.assertRaises(OSError):
            self.store.start_sync(stream, {"checkpoint_mode": "requests", "parameters": {},
                "include_defaults": False, "active_streams": [stream]}, clear_target=True)
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_stream_subscriptions"), [["0"]])
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_crud"), [["0"]])
        self.store.control("stop")

    def small_checkpoint(self, partial=False):
        body = {"id": ROOM, "status": "playing", "cols": 5, "rows": 4, "current_seat": 1,
                "score_p1": 0, "score_p2": 0, "winner": None, "move_count": 0}
        records = [
            {"sync_stream_started": {}},
            {"checkpoint": {"last_op_id": "1", "buckets": [
                {"bucket": "room", "checksum": 1, "priority": 3, "count": 1}]}},
            {"data": {"bucket": "room", "data": [operation(1, "rooms", body)]}},
        ]
        if partial:
            records.append({"partial_checkpoint_complete": {"priority": 3}})
        return records

    def test_whole_checkpoint_metadata_and_data_commit_once_with_full_marker(self):
        sync = make_sync(self.store)
        start(sync)
        records = self.small_checkpoint() + [{"checkpoint_complete": {}}]
        before = self.native.transactions
        results = self.store.control_lines([json.dumps(record) for record in records])
        self.assertEqual(self.native.transactions - before, 1)
        self.assertEqual([result["full_checkpoint"] for result in results], [False] * 3 + [True])
        for result in results[:-1]:
            sync._instructions(result["instructions"], full_checkpoint=result["full_checkpoint"])
        self.assertFalse(sync.synced)
        sync._instructions(results[-1]["instructions"], full_checkpoint=True)
        self.assertTrue(sync.synced)
        self.assertEqual(self.store.room(ROOM)["status"], "playing")
        sync.close()

    def test_real_partial_checkpoint_cannot_mark_ready_before_full_completion(self):
        sync = make_sync(self.store)
        start(sync)
        records = [{"checkpoint": {"last_op_id": "0", "buckets": []}},
                   {"partial_checkpoint_complete": {"priority": 3}}]
        results = self.store.control_lines([json.dumps(record) for record in records])
        for result in results:
            sync._instructions(result["instructions"], full_checkpoint=result["full_checkpoint"])
        self.assertFalse(sync.synced)
        results = self.store.control_lines([json.dumps({"checkpoint_complete": {}})])
        for result in results:
            sync._instructions(result["instructions"], full_checkpoint=result["full_checkpoint"])
        self.assertTrue(sync.synced)
        sync.close()

    def test_whole_checkpoint_commit_failure_rolls_back_rows_and_cursor(self):
        sync = make_sync(self.store)
        start(sync)
        original = self.store._exec

        def failed_commit(sql):
            if sql == "COMMIT":
                raise OSError("simulated failed checkpoint commit")
            return original(sql)

        records = self.small_checkpoint() + [{"checkpoint_complete": {}}]
        with patch.object(self.store, "_exec", side_effect=failed_commit), self.assertRaises(OSError):
            self.store.control_lines([json.dumps(record) for record in records])
        self.assertIsNone(self.store.room(ROOM))
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_oplog"), [["0"]])
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_buckets"), [["0"]])
        self.assertFalse(sync.synced)
        sync.close()

    def test_accepted_move_requires_exact_published_uuid_room_and_full_checkpoint(self):
        sync = make_sync(self.store, replies=[{"accepted": True}])
        start(sync)
        stream = sync.stream

        def send(records):
            data = b"".join(wire(record) for record in records)
            stream.parts = [data[i:i + 512] for i in range(0, len(data), 512)]
            for _ in range(100):
                sync.step(5)
                self.assertEqual(sync.phase, "stream", sync.last_error)
                if not stream.parts and sync.lines.offset == len(sync.lines.pending) and not sync.batch:
                    break
            else:
                self.fail("Native stream did not drain")

        empty = [{"checkpoint": {"last_op_id": "0", "buckets": []}}, {"checkpoint_complete": {}}]
        move = {"id": DEVICE, "orientation": "h", "row": 0, "col": 0}
        wrong_body = {"id": DEVICE, "room_id": USER, "seat": 1, "orientation": "h",
                      "row": 0, "col": 0, "move_index": 1}
        wrong = dict(operation(1, body=wrong_body), object_id=encode_uuid(DEVICE))
        right = dict(operation(2, body=dict(wrong_body, room_id=ROOM)), object_id=encode_uuid(DEVICE))
        room = operation(3, "rooms", {"id": ROOM, "status": "playing", "cols": 5, "rows": 4,
            "current_seat": 2, "score_p1": 0, "score_p2": 0, "winner": None, "move_count": 1})
        with patch.object(sync, "_checkpoint_request", side_effect=AssertionError("accepted move checkpoint HTTP")):
            send(empty)
            self.assertTrue(sync.synced)
            sync.records.save(dict(sync.records.record, pending_move=move))
            with patch.object(sync, "_post", wraps=sync._post) as upload:
                sync.step(1)
                sync.step(1)
            self.assertEqual(upload.call_count, 1)
            self.assertEqual(upload.call_args.args[1]["move_id"], DEVICE)
            self.assertEqual(sync.phase, "stream")
            self.assertIsNone(sync.target)
            self.assertEqual(sync.records.record["pending_move"], move)

            # A newer complete snapshot without the UUID is not confirmation.
            send(empty)
            self.assertFalse(sync.synced)
            self.assertEqual(sync.records.record["pending_move"], move)
            send([
                {"checkpoint": {"last_op_id": "1", "buckets": [
                    {"bucket": "other-room", "checksum": 1, "priority": 3, "count": 1}]}},
                {"data": {"bucket": "other-room", "data": [wrong]}},
                {"checkpoint_complete": {}},
            ])
            self.assertTrue(self.store.has_move(DEVICE, USER))
            self.assertFalse(self.store.has_move(DEVICE, ROOM))
            self.assertFalse(sync.synced)

            # Raw downloaded operations and partial progress must not clear the
            # saved UUID. The full checkpoint publishes move and board together.
            send([
                {"checkpoint": {"last_op_id": "3", "buckets": [
                    {"bucket": "paired-room", "checksum": 5, "priority": 3, "count": 2}]}},
                {"data": {"bucket": "paired-room", "data": [right, room]}},
                {"partial_checkpoint_complete": {"priority": 0}},
            ])
            self.assertGreater(int(self.native.exec("SELECT count(*) FROM ps_oplog")[0][0]), 0)
            self.assertFalse(self.store.has_move(DEVICE, ROOM))
            self.assertEqual(sync.records.record["pending_move"], move)
            self.assertFalse(sync.synced)
            send([{"checkpoint_complete": {}}])
            self.assertTrue(sync.synced, sync.last_error)
            self.assertIsNone(sync.records.record["pending_move"])
            self.assertTrue(self.store.has_move(DEVICE, ROOM))
            self.assertEqual(self.store.room(ROOM)["move_count"], 1)
            self.assertEqual(self.store.room(ROOM)["current_seat"], 2)
        self.assertIs(sync.stream, stream)
        self.assertFalse(stream.closed)
        sync.close()

    def test_older_checkpoint_waits_for_rejected_move_target_without_sql_crud(self):
        sync = make_sync(self.store)
        start(sync)
        stream = sync.stream
        self.store.seed_checkpoint("1")
        self.store.control("target_checkpoint_request_id", "3")
        sync.target = "3"
        for request_id, expected in (("2", False), ("3", True)):
            data = wire({"checkpoint": {"last_op_id": "0", "write_checkpoint": request_id, "buckets": []}})
            data += wire({"checkpoint_complete": {}})
            stream.parts = [data]
            sync.step(1)
            self.assertEqual(sync.phase, "stream", sync.last_error)
            self.assertEqual(sync.synced, expected)
            self.assertFalse(self.store.has_local_writes())
            self.assertFalse(stream.closed)
        sync.close()

    def test_complete_49_edge_board_fits_fixed_arena_and_database_and_reopens(self):
        self.store.bind_account(USER)
        sync = make_sync(self.store)
        start(sync)
        game = Game()
        edges = ([('h', row, col) for row in range(game.rows + 1) for col in range(game.cols)]
                 + [('v', row, col) for row in range(game.rows) for col in range(game.cols + 1)])
        operations = []
        for index, (orientation, row, col) in enumerate(edges, 1):
            seat = game.current_player
            game.claim(orientation, row, col)
            key = "%08x-4444-4444-8444-444444444444" % index
            operations.append(operation(index, body={"id": key, "room_id": ROOM, "seat": seat,
                "orientation": orientation, "row": row, "col": col, "move_index": index}))
        operations.append(operation(50, "rooms", {"id": ROOM, "status": "finished", "cols": 5,
            "rows": 4, "current_seat": game.current_player, "score_p1": game.scores[0],
            "score_p2": game.scores[1], "winner": game.winner, "move_count": 49}))
        data = wire({"checkpoint": {"last_op_id": "50", "write_checkpoint": TARGET,
            "buckets": [{"bucket": "room", "checksum": sum(range(1, 51)), "priority": 3, "count": 50}]}})
        data += wire({"data": {"bucket": "room", "data": operations}})
        data += wire({"checkpoint_complete": {"last_op_id": "50"}})
        sync.stream.parts = [data[i:i + 512] for i in range(0, len(data), 512)]
        before = self.native.transactions
        for _ in range(1000):
            sync.step(1)
            if sync.synced or sync.phase == "blocked":
                break
        self.assertEqual(sync.status, "SYNCED", sync.last_error)
        self.assertEqual(self.store.room(ROOM)["move_count"], 49)
        moves = self.store.moves(ROOM)
        self.assertEqual(len(moves), 49)
        self.assertLessEqual(self.native.max_rows, 16)
        self.assertLessEqual(self.native.transactions - before, 12)
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_crud"), [["0"]])
        self.assertLessEqual(int(self.native.exec("PRAGMA page_count")[0][0]), 96)
        sync.close()
        self.store.close()
        self.store.open()
        self.assertEqual(self.store.moves(ROOM), moves)
        self.assertFalse(self.store.bind_account(USER))

    def install_legacy_cache(self, version):
        import dots_storage
        # Recreate the old app's cache budget before installing its wider schema.
        self.store._exec("PRAGMA cache_size=8")
        def column(name, kind="integer"):
            return {"name": name, "type": kind}
        old_schema = {"tables": [
            {"name": "account", "local_only": True, "columns": [column("user_id", "text")]},
            {"name": "rooms", "columns": [column("status", "text")] + [column(name) for name in
                ("cols", "rows", "current_seat", "score_p1", "score_p2", "winner", "move_count")]},
            {"name": "moves", "columns": [column("room_id", "text"), column("seat"),
                column("orientation", "text"), column("row"), column("col"), column("move_index")]},
        ]}
        if version == 1:
            # Representative v1-only fields: the original full 34-column schema
            # cannot be created in this deliberately fixed 256KiB host arena.
            old_schema["tables"][1]["columns"].append(column("created_at", "text"))
            old_schema["tables"].append({"name": "room_players", "columns": [column("user_id", "text")]})
        elif version == 3:
            # Preserve a real compact JSON-view schema3 fixture after the
            # production schema moves to native tables. It must not inherit
            # today's SCHEMA constant or it would bypass the migration.
            old_schema["tables"][1:] = [
                {"name": "rooms", "columns": [column(name) for name in ("s", "p", "a", "b", "w", "n")]},
                {"name": "moves", "columns": [column("r", "text"), column("e"), column("n")]},
            ]
        self.store._exec("BEGIN IMMEDIATE")
        self.store._exec("SELECT powersync_clear(0)")
        for table in ("rooms", "moves"):
            if self.native.exec("SELECT type FROM sqlite_master WHERE name='%s'" % table) == [["table"]]:
                self.store._exec("DROP TABLE " + table)
        self.store._exec("SELECT powersync_replace_schema(%s)" % dots_storage._quote(json.dumps(old_schema)))
        self.store._exec("PRAGMA user_version=%d" % version)
        self.store._exec("COMMIT")
        self.store.bind_account(USER)
        self.store.control("subscriptions", {"subscribe": {
            "stream": {"name": "badge_game", "params": {"room_id": ROOM}}, "ttl": 300}})
        self.store.control("start", {"checkpoint_mode": "requests", "parameters": {},
                                    "schema": {"tables": [], "raw_tables": []}})
        room = operation(1, "rooms", {"id": ROOM, "status": "playing", "cols": 5, "rows": 4,
            "current_seat": 2, "score_p1": 0, "score_p2": 0, "winner": None, "move_count": 1}, compact=version == 3)
        move = operation(2, body={"id": DEVICE, "room_id": ROOM, "seat": 1,
            "orientation": "h", "row": 0, "col": 0, "move_index": 1}, compact=version == 3)
        self.store.control_lines([json.dumps(record) for record in checkpoint_records("old-room", [room, move])])
        self.store.control("stop")
        if version == 3:
            self.assertEqual(self.native.exec("SELECT s,n FROM rooms"), [["1", "1"]])
            self.assertEqual(self.native.exec("SELECT r FROM moves"), [[encode_uuid(ROOM)]])
        else:
            self.assertEqual(self.native.exec("SELECT status,move_count FROM rooms"), [["playing", "1"]])
            self.assertEqual(self.native.exec("SELECT room_id FROM moves"), [[ROOM]])
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_buckets"), [["1"]])
        return self.native.exec("SELECT value FROM ps_kv WHERE key='client_id'")

    def test_version_one_and_two_migrations_preserve_identity_pending_and_replace_only_cache(self):
        self.assert_legacy_migration((1, 2))

    def test_version_three_migration_preserves_identity_pending_and_rebuilds_raw_cache(self):
        self.assert_legacy_migration((3,))

    def assert_legacy_migration(self, versions):
        from dots_client import Records as SavedRecords
        for version in versions:
            with self.subTest(version=version):
                client_id = self.install_legacy_cache(version)
                session = SessionFile(self.store.root)
                move = {"id": DEVICE, "orientation": "h", "row": 0, "col": 0}
                session.save(dict(Records().record, pending_move=move))
                before = session.load(self.store.new_uuid)
                self.store.close()
                self.store.open()
                self.assertEqual(self.native.exec("PRAGMA user_version"), [["5"]])
                self.assertFalse(self.store.bind_account(USER))
                self.assertEqual(self.native.exec("SELECT value FROM ps_kv WHERE key='client_id'"), client_id)
                self.assertEqual(session.load(self.store.new_uuid), before)
                self.assertIsNone(self.store.room(ROOM))
                self.assertFalse(self.store.has_move(DEVICE, ROOM))
                for table in ("ps_buckets", "ps_oplog", "ps_stream_subscriptions", "ps_crud"):
                    self.assertEqual(self.native.exec("SELECT count(*) FROM " + table), [["0"]])
                sync = make_sync(self.store, records=SavedRecords(session, before), replies=[{"ok": True, "duplicate": True}])
                with patch.object(sync, "_checkpoint_request", side_effect=AssertionError("migration replay checkpoint HTTP")):
                    start(sync)
                    self.assertFalse(sync.synced)
                    self.deliver(sync, checkpoint_records("new-room", [
                        operation(1, body={"id": DEVICE, "room_id": ROOM, "seat": 1, "orientation": "h",
                            "row": 0, "col": 0, "move_index": 1}),
                        operation(2, "rooms", {"id": ROOM, "status": "playing", "cols": 5, "rows": 4,
                            "current_seat": 2, "score_p1": 0, "score_p2": 0, "winner": None, "move_count": 1}),
                    ]))
                self.assertTrue(sync.synced, sync.last_error)
                self.assertIsNone(session.load(self.store.new_uuid)["pending_move"])
                self.assertTrue(self.store.has_move(DEVICE, ROOM))
                self.assertEqual(self.store.moves(ROOM), [{"orientation": "h", "row": 0, "col": 0, "move_index": 1}])
                sync.close()

    def test_failed_compact_migration_rolls_back_old_cache_owner_and_version(self):
        self.assert_failed_legacy_migration(2)

    def test_failed_version_one_raw_migration_rolls_back_legacy_cache(self):
        self.assert_failed_legacy_migration(1)

    def test_failed_schema_three_raw_migration_rolls_back_json_views_and_metadata(self):
        self.assert_failed_legacy_migration(3)

    def assert_failed_legacy_migration(self, version):
        client_id = self.install_legacy_cache(version)
        session = SessionFile(self.store.root)
        saved = dict(Records().record, pending_move={"id": DEVICE, "orientation": "h", "row": 0, "col": 0})
        session.save(saved)
        self.store.close()
        original = self.store._exec
        def fail_commit(sql):
            if sql == "COMMIT":
                raise OSError("simulated failed migration commit")
            return original(sql)
        with patch.object(self.store, "_exec", side_effect=fail_commit), self.assertRaisesRegex(OSError, "failed migration"):
            self.store.open()
        self.native.open(self.store.root + "/state.sqlite")
        try:
            self.assertEqual(self.native.exec("PRAGMA user_version"), [[str(version)]])
            if version == 3:
                self.assertEqual(self.native.exec("SELECT s,n FROM rooms"), [["1", "1"]])
                self.assertEqual(self.native.exec("SELECT r FROM moves"), [[encode_uuid(ROOM)]])
            else:
                self.assertEqual(self.native.exec("SELECT status,move_count FROM rooms"), [["playing", "1"]])
            self.assertEqual(self.native.exec("SELECT type FROM sqlite_master WHERE name='rooms'"), [["view"]])
            self.assertEqual(self.native.exec("SELECT user_id FROM account WHERE id='self'"), [[USER]])
            self.assertEqual(self.native.exec("SELECT value FROM ps_kv WHERE key='client_id'"), client_id)
            self.assertEqual(session.load(self.store.new_uuid), saved)
        finally:
            self.native.close()
        self.store.open()
        self.assertEqual(self.native.exec("PRAGMA user_version"), [["5"]])
        self.assertIsNone(self.store.room(ROOM))
        self.assertFalse(self.store.bind_account(USER))

    def test_migrated_high_water_database_reuses_freelist_pages_for_sync(self):
        self.install_legacy_cache(2)
        self.store._exec("CREATE TABLE migration_filler (payload BLOB)")
        while int(self.native.exec("PRAGMA page_count")[0][0]) < 101:
            self.store._exec("INSERT INTO migration_filler VALUES(zeroblob(900))")
        self.store._exec("DROP TABLE migration_filler")
        self.store.close()
        self.store.open()
        self.assertGreater(int(self.native.exec("PRAGMA page_count")[0][0]), 96)
        self.assertLess(self.store._used_pages(), 96)
        sync = make_sync(self.store)
        start(sync)
        self.deliver(sync, checkpoint_records("compact-room", [operation(1, "rooms", {
            "id": ROOM, "status": "cancelled", "cols": 5, "rows": 4, "current_seat": 1,
            "score_p1": 0, "score_p2": 0, "winner": None, "move_count": 0})]))
        self.assertTrue(sync.synced, sync.last_error)
        self.assertEqual(self.store.room(ROOM)["status"], "cancelled")
        self.assertLessEqual(int(self.native.exec("PRAGMA page_count")[0][0]), 128)
        self.assertFalse(self.store.has_local_writes())
        sync.close()

    def test_invalid_batch_does_not_apply_earlier_valid_record(self):
        self.store.control("start", {"checkpoint_mode": "requests", "parameters": {}})
        self.store.control("seed_checkpoint_request_id", "1")
        good = wire({"data": {"bucket": "room", "data": [operation(1)]}}).decode().strip()
        bad = wire({"data": {"bucket": "room", "data": [operation(2, "unknown")]}}).decode().strip()
        before = self.native.exec("SELECT count(*) FROM ps_oplog")
        with self.assertRaisesRegex(ValueError, "Unexpected synced table"):
            self.store.control_batch([good, bad])
        self.assertEqual(self.native.exec("SELECT count(*) FROM ps_oplog"), before)


class FramingTests(unittest.TestCase):
    def parse_fragments(self, data, size=MAX_FRAGMENT):
        parser = StreamLines()
        actual = []
        for offset in range(0, len(data), size):
            parser.feed(data[offset:offset + size])
            while True:
                record = parser.pop_record()
                if record is None:
                    break
                self.assertEqual(json.loads(record[0]), record[1])
                actual.append(record)
        parser.finish()
        return actual

    def test_only_stream_reads_grow_and_request_writes_keep_the_original_bound(self):
        class Clock:
            @staticmethod
            def ticks_ms():
                return 1
            @staticmethod
            def ticks_diff(now, then):
                return now - then
        class Socket:
            def read(self, size):
                self.read_size = size
                return None
            def write(self, value):
                self.write_size = len(value)
                return len(value)
        for streaming in (False, True):
            task = HTTPS.__new__(HTTPS)
            task.time, task.heartbeat, task.streaming = Clock(), None, streaming
            task.response = Response()
            task.response.status = 200
            task.phase, task.started, task.last_data = "read", 0, 0
            task.secure = Socket()
            self.assertIs(task.step(), PENDING)
            self.assertEqual(task.secure.read_size, STREAM_READ_BYTES if streaming else READ_BYTES)
            task.phase, task.request, task.sent = "send", b"x" * 2048, 0
            self.assertIs(task.step(), PENDING)
            self.assertEqual(task.secure.write_size, 512)
        self.assertEqual(STREAM_READ_BYTES, MAX_FRAGMENT)

    def test_framing_reuses_the_decoded_record_and_preserves_raw_operation_text(self):
        raw = '{"op_id":"1", "op":"PUT", "object_type":"moves", "object_id":"x", "checksum":1, "data":"λ } , ] \\\""}'
        data = ('{"data":{"bucket":"λ", "data":[' + raw + ']}}\n').encode()
        records = self.parse_fragments(data, 7)
        self.assertEqual(len(records), 1)
        self.assertIn(raw, records[0][0])
        self.assertEqual(records[0][1]["data"]["bucket"], "λ")
        self.assertEqual(records[0][1]["data"]["data"][0], json.loads(raw))

    def test_quoted_spans_keep_escape_boundaries_and_reject_every_raw_control_byte(self):
        valid = {"text": 'λ' * 40 + ' escaped quote " slash \\ brackets {},[] end'}
        data = wire({"data": {"bucket": "r", "data": [operation(1, body=valid)]}})
        for size in (1, 2, 7, MAX_FRAGMENT):
            records = self.parse_fragments(data, size)
            self.assertEqual(records[0][1]["data"]["data"][0]["data"], json.dumps(valid))
        for control in range(32):
            for prefix, suffix in ((b'{"text":"', b'"}\n'),
                                   (b'{"data":{"bucket":"r","data":[{"text":"', b'"}]}}\n')):
                malformed = prefix + b"ordinary span" + bytes([control]) + b"ordinary span" + suffix
                with self.subTest(control=control, prefix=prefix), self.assertRaises(ValueError):
                    self.parse_fragments(malformed)

    def test_fragment_buffer_cannot_grow_or_be_overwritten_before_draining(self):
        parser = StreamLines()
        with self.assertRaisesRegex(ValueError, "fragment exceeds"):
            parser.feed(b" " * (MAX_FRAGMENT + 1))
        parser.feed(b'{}\n{}\n')
        self.assertEqual(parser.pop(), '{}')
        with self.assertRaisesRegex(ValueError, "Drain PowerSync"):
            parser.feed(b'{}\n')
        self.assertEqual(parser.pop(), '{}')
        self.assertIsNone(parser.pop())
        parser.finish()

    def test_malformed_truncated_and_oversized_records_remain_rejected(self):
        invalid = [b'[]\n', b'{"data":[]}\n', b'{"data":{"data":[],"bucket":"b"}}\n',
                   b'{"data":{"bucket":"b","bucket":"c","data":[]}}\n',
                   b'{"data":{"bucket":"b","data":[{},]}}\n',
                   b'{"data":{"bucket":"b","data":[1]}}\n',
                   b'{"data":{"bucket":"b","data":[{"x":tru}]}}\n',
                   b'{"data":{"bucket":"b","data":[]}}x\n',
                   b'{"x":"bad\\q"}\n', b'{"x":"bad\x01"}\n',
                   b'{"x":' + b'[' * MAX_DEPTH + b']' * MAX_DEPTH + b'}\n',
                   wire({"data": {"bucket": "x" * MAX_METADATA, "data": []}}),
                   wire({"data": {"bucket": "b", "data": [{"x": "y" * MAX_LINE}]}}),
                   wire({"x": "y" * MAX_LINE})]
        for data in invalid:
            with self.subTest(prefix=data[:100]), self.assertRaises((ValueError, OSError)):
                self.parse_fragments(data, 7)
        for data in (b'{"x":1}', b'{"data":{"bucket":"b","data":[{}'):
            with self.subTest(data=data), self.assertRaises(OSError):
                self.parse_fragments(data)

    def test_fragmented_http_chunks_preserve_the_body(self):
        response = Response()
        body = b'{"data":{"bucket":"b","data":[]}}\n'
        data = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        for part in (body[:10], body[10:]):
            data += ("%x\r\n" % len(part)).encode() + part + b"\r\n"
        data += b"0\r\n\r\n"
        for size in (1, 7, 512, MAX_FRAGMENT):
            response = Response()
            actual = b"".join(response.feed(data[i:i + size]) for i in range(0, len(data), size))
            response.finish()
            self.assertEqual(actual, body)

    def test_many_http_chunks_in_one_read_keep_only_the_incomplete_suffix(self):
        body = bytes(range(256)) * 4
        framed = b"".join(b"1\r\n" + bytes([value]) + b"\r\n" for value in body) + b"0\r\n\r\n"
        for size in (1, 7, 512, STREAM_READ_BYTES):
            response = Response()
            response.feed(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
            actual = []
            for offset in range(0, len(framed), size):
                actual.append(response.feed(framed[offset:offset + size]))
                self.assertLessEqual(len(response.buffer), 2)
            response.finish()
            self.assertEqual(b"".join(actual), body)

    def test_ambiguous_malformed_and_truncated_http_framing_is_rejected(self):
        invalid = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nContent-Length: 1\r\n\r\nx",
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\nTransfer-Encoding: chunked\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nxx",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;ext=bad\r\nx\r\n0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nx--0\r\n\r\n",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\nextra\r\n\r\n",
        ]
        for data in invalid:
            for size in (1, STREAM_READ_BYTES):
                with self.subTest(data=data, size=size), self.assertRaises((ValueError, OSError)):
                    response = Response()
                    for offset in range(0, len(data), size):
                        response.feed(data[offset:offset + size])
                    response.finish()
        for suffix in (b"1\r\nx", b"0\r\n\r", b"2\r\nx"):
            response = Response()
            response.feed(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + suffix)
            with self.assertRaises(OSError):
                response.finish()

    def test_large_service_line_preserves_operations_at_every_fragment_size(self):
        operations = [operation(i, body={"text": 'quote " Unicode λ },] ' * 8}) for i in range(30)]
        data = wire({"data": {"bucket": "room", "data": operations}})
        self.assertGreater(len(data), 8192)
        for size in (1, 7, 512, MAX_FRAGMENT):
            parser = StreamLines()
            actual = []
            for offset in range(0, len(data), size):
                parser.feed(data[offset:offset + size])
                while True:
                    line = parser.pop()
                    if line is None:
                        break
                    actual.extend(json.loads(line)["data"]["data"])
            parser.finish()
            self.assertEqual(actual, operations)


if __name__ == "__main__":
    unittest.main()
