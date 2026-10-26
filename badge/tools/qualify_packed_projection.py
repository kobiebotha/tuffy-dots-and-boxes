#!/usr/bin/env python3
"""Local packed-room adapter/receipt proof using the pinned 256 KiB core.

Only temporary POSIX databases are written. The production Store schema,
subscription supervisor and board adapter are never modified or installed.
"""
import ctypes as c
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "badge/tests"), str(ROOT / "sqlite-shadow-vfs"), str(ROOT / "badge/lib"), str(ROOT / "badge/app/dots_boxes")]
import dots_storage as storage
from powersync.database import MAX_SYNC_BATCH_RECORDS, MAX_SYNC_LINE_BYTES
from dots_codec import decode_edge, encode_uuid
from dots_session import fresh_session
from game import Game
from native_fixture import HostCore, LIBRARY


def compact(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class Experiment:
    def __init__(self, fixture, inputs, parameters=None):
        self.fixture, self.inputs = fixture, inputs
        self.temp = tempfile.TemporaryDirectory(prefix="dots-packed-native-")
        self.native = HostCore()
        self.original_schema = storage.SCHEMA
        storage.SCHEMA = compact({"tables": [{"name": "account", "local_only": True,
            "columns": [{"name": "user_id", "type": "text"}]}], "raw_tables": [
                {"name": name, "table_name": name, "clear": "DELETE FROM " + name}
                for name in ("rooms", "moves")]})
        self.store = storage.Store(native=self.native, root=self.temp.name + "/dots").open()
        self.fields = ["id", "s", "n"] if fixture["name"] != "current" else ["id", "s", "p", "a", "b", "w", "n"]
        if fixture["name"].startswith("packed"):
            self.fields.append("h")
        # This schema's clear callbacks can match APP_SCHEMA exactly, in which
        # case open created its two empty production tables. Replace only these
        # disposable experimental tables before any sync or account binding.
        for table in ("rooms", "moves"):
            self.native.exec("DROP TABLE IF EXISTS " + table)
        self.native.exec("CREATE TABLE rooms (" + ",".join(name + (
            " TEXT PRIMARY KEY NOT NULL" if name == "id" else " TEXT" if name == "h" else " INTEGER")
            for name in self.fields) + ") WITHOUT ROWID")
        self.native.exec("CREATE TABLE moves (id TEXT PRIMARY KEY NOT NULL,r TEXT,e INTEGER,n INTEGER) WITHOUT ROWID")
        self.store.bind_account(inputs["userId"])
        self.stream = {"name": "experiment", "params": parameters or {
            "room_id": inputs["roomId"], "pending_move_id": None}}
        self.store.start_sync(self.stream, {"checkpoint_mode": "requests", "include_defaults": False,
            "parameters": {}, "active_streams": [self.stream]}, clear_target=True)
        self.stats = {}
        self.completions = 0
        self.last_op_id = "0"

    def close(self):
        try:
            self.store.close()
        finally:
            storage.SCHEMA = self.original_schema
            self.temp.cleanup()

    def rows(self, table):
        names = self.fields if table == "rooms" else ["id", "r", "e", "n"]
        result, offset = [], 0
        while True:
            page = self.native.exec("SELECT " + ",".join(names) + " FROM " + table +
                " ORDER BY id LIMIT 16 OFFSET " + str(offset))
            result.extend(dict(zip(names, row)) for row in page)
            if len(page) < 16:
                return result
            offset += len(page)

    def subscription(self, parameters):
        stream = {"name": "experiment", "params": parameters}
        self.store.control_group([
            ("subscriptions", {"unsubscribe": {"name": "experiment", "params": self.stream["params"]}}),
            ("subscriptions", {"subscribe": {"stream": stream, "ttl": 300}}),
        ])
        self.stream = stream

    def apply(self, checkpoint, complete=True):
        if checkpoint.get("subscription"):
            self.subscription(checkpoint["parameters"])
        grouped = {}
        for item in checkpoint["operations"]:
            bucket, op = item["bucket"], item["operation"]
            checksum, count = self.stats.get(bucket, (0, 0))
            self.stats[bucket] = ((checksum + op["checksum"]) & 0xffffffff, count + 1)
            grouped.setdefault(bucket, []).append(op)
        self.last_op_id = checkpoint["last_op_id"]
        records = [compact({"checkpoint": {"last_op_id": self.last_op_id, "buckets": [
            {"bucket": bucket, "checksum": self.stats.get(bucket, (0, 0))[0],
             "count": self.stats.get(bucket, (0, 0))[1], "priority": 3}
            for bucket in checkpoint["buckets"]]}})]
        records.extend(compact({"data": {"bucket": bucket, "data": [op]}})
            for bucket, ops in grouped.items() for op in ops)
        if complete:
            records.append(compact({"checkpoint_complete": {}}))
        batch, size = [], 0
        for record in records:
            length = len(record.encode())
            if batch and (len(batch) == MAX_SYNC_BATCH_RECORDS or size + length > MAX_SYNC_LINE_BYTES):
                self.deliver(batch)
                batch, size = [], 0
            batch.append(record)
            size += length
        if batch:
            self.deliver(batch)

    def deliver(self, records):
        for result in self.store.control_lines(records):
            self.completions += sum("DidCompleteSync" in item for item in result["instructions"])
            assert not any("CloseSyncStream" in item for item in result["instructions"]), result

    def replay(self):
        rooms = self.rows("rooms")
        assert len(rooms) == 1, rooms
        room = rooms[0]
        assert room["id"] == encode_uuid(self.inputs["roomId"])
        count = int(room["n"])
        game = Game()
        if "h" in room:
            history = room["h"]
            assert len(history) == count <= 49 and len(set(history)) == count
            codes = [self.inputs["alphabet"].index(char) for char in history]
        else:
            rows = sorted(self.rows("moves"), key=lambda row: int(row["n"]))
            assert len(rows) == count
            assert all(row["r"] == room["id"] for row in rows)
            assert [int(row["n"]) for row in rows] == list(range(1, count + 1))
            codes = [int(row["e"]) for row in rows]
        for edge in codes:
            assert game.claim(*decode_edge(edge)) is not None
        expected = self.inputs["roomStates"][count]
        assert game.moves == count and game.current_player == expected["current_seat"]
        assert game.scores == [expected["score_p1"], expected["score_p2"]]
        assert game.winner == expected["winner"]
        if "p" in room:
            assert [int(room[key]) for key in ("p", "a", "b")] == [game.current_player, *game.scores]
        return count

    def acknowledged(self, move, full_checkpoint=True):
        if not full_checkpoint:
            return False
        if not self.store.has_move(move["id"], self.inputs["roomId"]):
            return False
        room = self.rows("rooms")[0]
        receipt = next(row for row in self.rows("moves") if row["id"] == encode_uuid(move["id"]))
        assert receipt["r"] == room["id"] and int(receipt["n"]) <= int(room["n"])
        if "h" in room:
            assert self.inputs["alphabet"].index(room["h"][int(receipt["n"]) - 1]) == int(receipt["e"])
        return True


def measure(fixture, name, scenario, inputs):
    test = Experiment(fixture, inputs, scenario["checkpoints"][0]["parameters"])
    try:
        start = test.native.transactions
        memory = test.native.library.sqlite3_memory_highwater
        memory.argtypes, memory.restype = [c.c_int], c.c_longlong
        memory(1)
        for checkpoint in scenario["checkpoints"]:
            test.apply(checkpoint)
            assert test.replay() == checkpoint["expectedMoveCount"]
            pending = checkpoint["parameters"]["pending_move_id"]
            if pending and any(move["id"] == pending for move in inputs["moves"][:checkpoint["expectedMoveCount"]]):
                move = next(move for move in inputs["moves"] if move["id"] == pending)
                assert test.acknowledged(move)
                assert not test.store.has_move(pending, "650e8400-e29b-41d4-a716-446655440000")
        assert test.completions == len(scenario["checkpoints"])
        assert test.native.exec("SELECT count(*) FROM ps_crud") == [["0"]]
        assert test.native.exec("PRAGMA quick_check") == [["ok"]]
        return {"variant": fixture["name"], "scenario": name,
            "transactions": test.native.transactions - start, "completedCheckpoints": test.completions,
            "receiptRows": len(test.rows("moves")), "peakNativeBytes": memory(0),
            "databaseBytes": Path(test.store.root + "/state.sqlite").stat().st_size,
            "maxReturnedRows": test.native.max_rows}
    finally:
        test.close()


def proofs(fixture, inputs):
    checkpoint = fixture["scenarios"]["fresh"]["checkpoints"][0]
    test = Experiment(fixture, inputs, checkpoint["parameters"])
    try:
        pending = next(move for move in inputs["moves"] if move["id"] == fixture["finalPending"])
        test.apply(checkpoint, complete=False)
        assert test.rows("rooms") == [] and not test.acknowledged(pending)
        test.deliver([compact({"partial_checkpoint_complete": {"priority": 0}})])
        assert not test.acknowledged(pending)
        test.deliver([compact({"partial_checkpoint_complete": {"priority": 3}})])
        assert not test.acknowledged(pending, full_checkpoint=False)
        test.deliver([compact({"checkpoint_complete": {}})])
        assert test.acknowledged(pending) and test.replay() == 49
        # A full checkpoint deleting the active receipt bucket must retire its
        # published proof; a cached packed history alone cannot confirm a UUID.
        receipt_buckets = {row["bucket"] for row in checkpoint["rows"] if row["table"] == "moves"}
        keep = [bucket for bucket in checkpoint["buckets"] if bucket not in receipt_buckets]
        test.deliver([compact({"checkpoint": {"last_op_id": test.last_op_id, "buckets": [
            {"bucket": bucket, "checksum": test.stats[bucket][0], "count": test.stats[bucket][1], "priority": 3}
            for bucket in keep]}}), compact({"checkpoint_complete": {}})])
        assert not test.acknowledged(pending) and test.rows("moves") == []
        assert test.replay() == 49
        # Restore exact UUID, then exercise row REMOVE within a retained bucket.
        for bucket in receipt_buckets:
            test.stats.pop(bucket, None)
        restored = dict(checkpoint, operations=[])
        for item in checkpoint["operations"]:
            if item["operation"]["object_type"] == "moves":
                op = dict(item["operation"], op_id=str(int(test.last_op_id) + len(restored["operations"]) + 1))
                restored["operations"].append({"bucket": item["bucket"], "operation": op})
        restored["last_op_id"] = restored["operations"][-1]["operation"]["op_id"]
        test.apply(restored)
        assert test.acknowledged(pending)
        bucket = next(row["bucket"] for row in checkpoint["rows"] if row["table"] == "moves")
        op_id = str(int(test.last_op_id) + 1)
        checksum, count = test.stats[bucket]
        remove = {"op_id": op_id, "op": "REMOVE", "object_type": "moves",
                  "object_id": encode_uuid(pending["id"]), "checksum": 17}
        buckets = [{"bucket": name, "checksum": ((value[0] + 17) & 0xffffffff) if name == bucket else value[0],
                    "count": value[1] + (1 if name == bucket else 0), "priority": 3}
                   for name, value in test.stats.items() if name in checkpoint["buckets"]]
        test.deliver([compact({"checkpoint": {"last_op_id": op_id, "buckets": buckets}}),
            compact({"data": {"bucket": bucket, "data": [remove]}})])
        assert test.acknowledged(pending)
        test.deliver([compact({"checkpoint_complete": {}})])
        assert not test.acknowledged(pending)
        # Restore a receipt with a new oplog id, then reset accounts while both
        # published tables are nonempty. No stale proof may survive the switch.
        restore_op = dict(restored["operations"][0]["operation"], op_id=str(int(op_id) + 1))
        restore = dict(restored, operations=[{"bucket": bucket, "operation": restore_op}], last_op_id=restore_op["op_id"])
        test.stats[bucket] = ((checksum + 17) & 0xffffffff, count + 1)
        test.apply(restore)
        assert test.acknowledged(pending)
        # Account reconciliation invokes the experiment's actual raw-table clear
        # callbacks, keeping another account from observing a cached board/proof.
        replacement = dict(fresh_session("33333333-3333-4333-8333-333333333333"),
            user_id=inputs["peerId"], room_id=inputs["roomId"], seat=2,
            access_token="fixture", refresh_token="fixture", expires_at=10**12)
        assert test.store.reconcile_session(replacement)
        assert test.rows("rooms") == [] and test.rows("moves") == []
        assert not test.store.has_move(pending["id"], inputs["roomId"])
        result = {"fullCheckpointRequired": True, "partialCannotAcknowledge": True,
                "exactUuidAndRoomRequired": True, "retiredBucketRemovesReceipt": True,
                "rowRemovePublishesOnlyAtFullCheckpoint": True, "accountResetClearsBoardAndReceipt": True}
    finally:
        test.close()
    # A reboot can recover an old accepted UUID after many subsequent moves.
    # Exact receipt selection must still confirm that UUID, not only the latest.
    checkpoint = fixture["recovery"]
    test = Experiment(fixture, inputs, checkpoint["parameters"])
    try:
        oldest = inputs["moves"][0]
        test.apply(checkpoint)
        assert test.acknowledged(oldest) and test.replay() == 49
        assert not test.acknowledged(inputs["moves"][-1])
        test.store.close()
        test.native = HostCore()
        test.store = storage.Store(native=test.native, root=test.temp.name + "/dots").open()
        assert not test.store.bind_account(inputs["userId"])
        assert test.acknowledged(oldest) and test.replay() == 49
        test.store.start_sync(test.stream, {"checkpoint_mode": "requests", "include_defaults": False,
            "parameters": {}, "active_streams": [test.stream]}, clear_target=True)
        # Membership revocation removes every authorized bucket at publication.
        test.deliver([compact({"checkpoint": {"last_op_id": test.last_op_id, "buckets": []}})])
        assert test.acknowledged(oldest)
        test.deliver([compact({"checkpoint_complete": {}})])
        assert test.rows("rooms") == [] and test.rows("moves") == []
        result.update({"olderAcceptedUuidRecovery": True, "membershipRevocationClearsOnlyAtCheckpoint": True})
        return result
    finally:
        test.close()


def main():
    assert LIBRARY.exists(), "Pinned qualifier unavailable"
    inputs = json.loads(Path(sys.argv[1]).read_text())
    results = [measure(fixture, name, scenario, inputs) for fixture in inputs["fixtures"]
        for name, scenario in fixture["scenarios"].items()]
    dynamic = next(fixture for fixture in inputs["fixtures"] if fixture["name"] == "packed_dynamic")
    print(compact({"arenaBytes": 256 * 1024, "results": results, "proofs": proofs(dynamic, inputs)}))


if __name__ == "__main__":
    main()
