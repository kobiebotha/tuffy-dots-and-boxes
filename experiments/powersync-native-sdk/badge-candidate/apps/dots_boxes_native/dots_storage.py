"""Raw canonical game tables and ordinary SDK-managed local mutations."""
import json
import os

from dots_session import FILES as SESSION_FILES, SessionFile, is_uuid, validate_session
from dots_codec import COLS, ROWS, decode_edge, decode_status, encode_uuid
from powersync.database import Database, _quote

ROOT = "/state/dots_boxes_native"
OWNER = "dots-boxes-native-sdk-local-v1\n"
MAX_MOVES = 64
QUERY_ROWS = 16
APP_SCHEMA = (
    '{"tables":['
    '{"name":"account","local_only":true,"columns":[{"name":"user_id","type":"text"}]},'
    '{"name":"_badge_move_outbox","columns":['
    '{"name":"room_id","type":"text"},{"name":"orientation","type":"text"},'
    '{"name":"row_i","type":"integer"},{"name":"col_i","type":"integer"}]}],'
    '"raw_tables":['
    '{"name":"rooms","table_name":"rooms","clear":"DELETE FROM rooms"},'
    '{"name":"moves","table_name":"moves","clear":"DELETE FROM moves"}]}'
)
SCHEMA = APP_SCHEMA
# SQLite update_hook (used by SDK watch_tables) does not report WITHOUT ROWID
# changes. Keep raw tables rowid-backed so normal SDK watches observe sync.
RAW_TABLE_SQL = (
    "CREATE TABLE rooms (id TEXT PRIMARY KEY NOT NULL, "
    "s INTEGER,p INTEGER,a INTEGER,b INTEGER,w INTEGER,n INTEGER)",
    "CREATE TABLE moves (id TEXT PRIMARY KEY NOT NULL, r TEXT,e INTEGER,n INTEGER)",
)
SYNCED_TYPES = ("rooms", "moves")


def _number(value):
    return None if value is None else int(value)


class QueryWatch:
    """Query snapshots driven by the native SDK's committed table watches."""
    def __init__(self, store, room_id, identity):
        self.store, self.room_id, self.identity = store, room_id, identity
        self.dirty, self.closed = True, False
        store.native.sdk_reply(json.dumps({"op": "watch", "id": identity,
            "tables": ["rooms", "moves", "_badge_move_outbox"]}))

    def invalidate(self):
        self.dirty = True

    def read(self):
        if self.closed or not self.dirty:
            return None
        value = self.store.snapshot(self.room_id)
        self.dirty = False
        return value

    def close(self):
        if not self.closed:
            self.store.native.sdk_reply(json.dumps({"op": "unwatch", "id": self.identity}))
            self.store._watches.pop(self.identity, None)
            self.closed = True


class Store(Database):
    def __init__(self, heartbeat=None, native=None, root=ROOT):
        super().__init__(SCHEMA, root, OWNER, SESSION_FILES, SYNCED_TYPES,
                         heartbeat=heartbeat, native=native, schema_version=7,
                         previous_versions=(1, 2, 3, 4, 5, 6))
        self._watches = {}
        self._next_watch = 1

    def _page_all(self, sql):
        rows = []
        while True:
            page = self._exec(sql + " LIMIT %d OFFSET %d" % (QUERY_ROWS, len(rows)))
            rows.extend(page)
            if len(rows) > MAX_MOVES:
                raise ValueError("Too many rows for this badge")
            if len(page) < QUERY_ROWS:
                return rows

    def _initialize(self, version):
        rebuilding = version is None or version in ("1", "2", "3")
        queued = self._validate_local_writes() if version is not None else []
        receipts = {}
        if version == "6":
            for row in self._page_all("SELECT id,result FROM _badge_move_receipts ORDER BY id"):
                receipts[row[0]] = row[1]
            if any(receipts.get(move["id"]) == "rejected" for move in queued):
                raise ValueError("Finish the old rejected SDK upload before upgrading")
        legacy = None
        if version != "7":
            try:
                os.stat(self.root + "/session.json")
            except OSError as error:
                if error.args[0] != 2:
                    raise
            else:
                legacy = SessionFile(self.root).load(None)
        if rebuilding and version is not None:
            if queued:
                raise ValueError("Cannot rebuild a cache with SDK uploads")
            self._exec("SELECT powersync_clear(0)")
        self._replace_schema()
        if self.schema == APP_SCHEMA:
            if rebuilding:
                for statement in RAW_TABLE_SQL:
                    self._exec(statement)
            else:
                for table, columns, statement in zip(("rooms", "moves"),
                        ("id,s,p,a,b,w,n", "id,r,e,n"), RAW_TABLE_SQL):
                    definition = self._scalar("SELECT sql FROM sqlite_master WHERE type='table' AND name=" + _quote(table))
                    if "WITHOUT ROWID" not in definition.upper():
                        continue
                    rows = self._page_all("SELECT %s FROM %s ORDER BY id" % (columns, table))
                    self._exec("DROP TABLE " + table)
                    self._exec(statement)
                    for row in rows:
                        values = ",".join("NULL" if value is None else _quote(value) for value in row)
                        self._exec("INSERT INTO %s(%s) VALUES(%s)" % (table, columns, values))
        if legacy is not None and legacy["pending_move"] is not None:
            move = self.validate_move(dict(legacy["pending_move"], room_id=legacy["room_id"]))
            account = self._exec("SELECT user_id FROM account WHERE id='self'")
            if account and account[0][0] != legacy["user_id"]:
                raise ValueError("Legacy mutation belongs to another account")
            matching = [value for value in queued if value["id"] == move["id"]]
            if matching and matching != [move]:
                raise ValueError("Legacy mutation differs from SDK queue")
            if (not matching and receipts.get(move["id"]) not in ("success", "rejected")
                    and not self.has_move(move["id"], move["room_id"])):
                # One-time handoff of a reset between old file-save and enqueue.
                # user_version and this SDK INSERT commit in the same open tx.
                self._insert_move(move)
        if version != "7":
            self._exec("PRAGMA user_version=7")

    @staticmethod
    def validate_move(move):
        from dots_codec import encode_edge
        if (not isinstance(move, dict) or set(move) != {"id", "room_id", "orientation", "row", "col"}
                or not is_uuid(move["id"]) or not is_uuid(move["room_id"])):
            raise ValueError("Invalid SDK move")
        encode_edge(move["orientation"], move["row"], move["col"])
        return move

    def _validate_local_writes(self):
        moves = []
        for row in self._page_all("SELECT data FROM ps_crud ORDER BY id"):
            value = json.loads(row[0])
            if (not isinstance(value, dict) or set(value) != {"op", "type", "id", "data"}
                    or value["op"] != "PUT" or value["type"] != "_badge_move_outbox"
                    or not isinstance(value["data"], dict)
                    or set(value["data"]) != {"room_id", "orientation", "row_i", "col_i"}):
                raise ValueError("Unexpected SDK CRUD; retained unchanged")
            data = value["data"]
            move = self.validate_move({"id": value["id"], "room_id": data["room_id"],
                "orientation": data["orientation"], "row": data["row_i"], "col": data["col_i"]})
            if any(previous["id"] == move["id"] for previous in moves):
                raise ValueError("Duplicate SDK mutation")
            moves.append(move)
        return moves

    def _insert_move(self, move):
        self._exec("INSERT INTO _badge_move_outbox(id,room_id,orientation,row_i,col_i) VALUES(%s,%s,%s,%d,%d)" %
                   (_quote(move["id"]), _quote(move["room_id"]), _quote(move["orientation"]), move["row"], move["col"]))

    def mutate_move(self, room_id, orientation, row, col):
        move = self.validate_move({"id": self.new_uuid(), "room_id": room_id,
                                   "orientation": orientation, "row": row, "col": col})
        self._check_reserve()
        self._insert_move(move)
        return move["id"]

    def snapshot(self, room_id):
        room = self.room(room_id)
        moves = self.moves(room_id)
        local = self._page_all("SELECT id,orientation,row_i,col_i FROM _badge_move_outbox WHERE room_id=%s ORDER BY id" % _quote(room_id))
        for identity, orientation, row, col in local:
            move = self.validate_move({"id": identity, "room_id": room_id,
                "orientation": orientation, "row": int(row), "col": int(col)})
            if not self.has_move(identity, room_id):
                moves.append({"orientation": orientation, "row": move["row"], "col": move["col"],
                              "move_index": len(moves) + 1})
        return {"room": room, "moves": moves, "local_moves": len(local)}

    def watch(self, room_id):
        if not is_uuid(room_id) or len(self._watches) >= 4:
            raise ValueError("Invalid or excessive query watch")
        identity = self._next_watch
        self._next_watch += 1
        watch = QueryWatch(self, room_id, identity)
        self._watches[identity] = watch
        return watch

    def notify_watches(self, identities):
        for identity in identities:
            watch = self._watches.get(identity)
            if watch is not None:
                watch.invalidate()

    def bind_account(self, user_id):
        if not is_uuid(user_id):
            raise ValueError("Invalid account identity")
        rows = self._exec("SELECT user_id FROM account WHERE id='self'")
        if rows:
            if rows[0][0] != user_id:
                raise ValueError("This badge is paired to another account")
            return False
        self._check_reserve()
        self._exec("INSERT INTO account(id,user_id) VALUES('self',%s)" % _quote(user_id))
        return True

    def reconcile_session(self, record):
        validate_session(record)
        user_id = record["user_id"]
        if user_id is None or record["refresh_token"] is None:
            raise ValueError("A paired session is required")
        for move in self._validate_local_writes():
            if move["room_id"] != record["room_id"]:
                raise ValueError("SDK mutation belongs to another room")
        local = self._page_all("SELECT room_id FROM _badge_move_outbox ORDER BY id")
        if any(row[0] != record["room_id"] for row in local):
            raise ValueError("SDK local rows belong to another room")
        rows = self._exec("SELECT user_id FROM account WHERE id='self'")
        if not rows or rows[0][0] == user_id:
            return self.bind_account(user_id)
        if self.has_local_writes() or local:
            raise ValueError("SDK mutations belong to another account")
        self._check_reserve()
        self.control("stop")
        self._exec("BEGIN IMMEDIATE")
        try:
            self._exec("SELECT powersync_clear(0)")
            self._exec("UPDATE account SET user_id=%s WHERE id='self'" % _quote(user_id))
            self._exec("COMMIT")
        except Exception:
            self._rollback()
            raise
        return True

    def room(self, room_id):
        if not is_uuid(room_id):
            raise ValueError("Invalid room")
        rows = self._exec(
            "SELECT s,p,a,b,w,n FROM rooms WHERE id=%s" % _quote(encode_uuid(room_id))
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "status": decode_status(_number(row[0])),
            "cols": COLS,
            "rows": ROWS,
            "current_seat": _number(row[1]),
            "score_p1": _number(row[2]),
            "score_p2": _number(row[3]),
            "winner": _number(row[4]),
            "move_count": _number(row[5]),
        }

    def moves(self, room_id, after=0):
        if not is_uuid(room_id):
            raise ValueError("Invalid room")
        if type(after) is not int or not 0 <= after <= MAX_MOVES:
            raise ValueError("Invalid move cursor")
        cursor = after
        rows = []
        while True:
            page = self._exec(
                "SELECT e,n FROM moves WHERE r=%s "
                "AND n>%d ORDER BY n LIMIT %d" % (_quote(encode_uuid(room_id)), cursor, QUERY_ROWS)
            )
            for edge, index in page:
                if index is None or int(index) <= cursor:
                    raise ValueError("Invalid move order")
                cursor = int(index)
            rows.extend(page)
            if len(rows) > MAX_MOVES:
                raise ValueError("Too many moves for this board")
            if len(page) < QUERY_ROWS:
                break
        result = []
        for row in rows:
            orientation, row_i, col_i = decode_edge(_number(row[0]))
            result.append({"orientation": orientation, "row": row_i, "col": col_i,
                           "move_index": _number(row[1])})
        return result

    def has_move(self, move_id, room_id):
        """Read only the published immutable move, never the internal oplog."""
        if not is_uuid(move_id) or not is_uuid(room_id):
            raise ValueError("Invalid move identity")
        return self._scalar("SELECT count(*) FROM moves WHERE id=%s AND r=%s" % (
            _quote(encode_uuid(move_id)), _quote(encode_uuid(room_id)))) == "1"
