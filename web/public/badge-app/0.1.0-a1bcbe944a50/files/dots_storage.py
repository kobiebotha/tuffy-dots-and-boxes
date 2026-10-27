"""Bounded game database on Badgeware's LittleFS mount.

Synced rows arrive only through PowerSync. The local account row records which
web user this file belongs to. A leftover journal or unknown file is retained.
"""
from dots_session import FILES as SESSION_FILES
from dots_session import is_uuid, validate_session
from dots_codec import COLS, ROWS, decode_edge, decode_status, encode_uuid

from powersync.database import Database, _quote

ROOT = "/state/dots_boxes"
OWNER = "dots-boxes-app-local-v1\n"
MAX_MOVES = 64
QUERY_ROWS = 16
APP_SCHEMA = (
    '{"tables":['
    '{"name":"account","local_only":true,"columns":[{"name":"user_id","type":"text"}]}],'
    '"raw_tables":['
    '{"name":"rooms","table_name":"rooms","clear":"DELETE FROM rooms"},'
    '{"name":"moves","table_name":"moves","clear":"DELETE FROM moves"}]}'
)
SCHEMA = APP_SCHEMA
RAW_TABLE_SQL = (
    "CREATE TABLE rooms (id TEXT PRIMARY KEY NOT NULL, "
    "s INTEGER,p INTEGER,a INTEGER,b INTEGER,w INTEGER,n INTEGER) WITHOUT ROWID",
    "CREATE TABLE moves (id TEXT PRIMARY KEY NOT NULL, r TEXT,e INTEGER,n INTEGER) WITHOUT ROWID",
)
SYNCED_TYPES = ("rooms", "moves")


def _number(value):
    if value is None:
        return None
    return int(value)


class Store(Database):
    """Game schema, cache migrations and queries on the SDK database."""
    def __init__(self, heartbeat=None, native=None, root=ROOT):
        super().__init__(SCHEMA, root, OWNER, SESSION_FILES, SYNCED_TYPES,
                         heartbeat=heartbeat, native=native, schema_version=5,
                         previous_versions=(1, 2, 3, 4))

    def _initialize(self, version):
        rebuilding = version is None or version in ("1", "2", "3")
        if rebuilding and version is not None:
            # Only synchronized cache is replaced. Local identity and the
            # separately saved pairing/pending move survive interrupted upgrades.
            self._exec("SELECT powersync_clear(0)")
        self._replace_schema()
        if self.schema == APP_SCHEMA:
            if rebuilding:
                for statement in RAW_TABLE_SQL:
                    self._exec(statement)
            elif version == "4":
                # Copy one bounded game table into RAM before rebuilding it.
                # Reuse its freed pages: growing temporary tables would require
                # file truncation on rollback, which this VFS cannot provide.
                for table, columns, statement in zip(("rooms", "moves"),
                        ("id,s,p,a,b,w,n", "id,r,e,n"), RAW_TABLE_SQL):
                    rows = []
                    while True:
                        page = self._exec("SELECT %s FROM %s ORDER BY id LIMIT %d OFFSET %d" %
                                          (columns, table, QUERY_ROWS, len(rows)))
                        rows.extend(page)
                        if len(rows) > MAX_MOVES:
                            raise ValueError("Too many cached rows to migrate")
                        if len(page) < QUERY_ROWS:
                            break
                    self._exec("DROP TABLE " + table)
                    self._exec(statement)
                    for row in rows:
                        values = ",".join("NULL" if value is None else _quote(value) for value in row)
                        self._exec("INSERT INTO %s(%s) VALUES(%s)" % (table, columns, values))
        if version != "5":
            self._exec("PRAGMA user_version=5")

    def bind_account(self, user_id):
        if not is_uuid(user_id):
            raise ValueError("Invalid account identity")
        rows = self._exec("SELECT user_id FROM account WHERE id='self'")
        if rows:
            if rows[0][0] != user_id:
                raise ValueError("This badge is paired to another account")
            return False
        self._check_reserve()
        self._exec("BEGIN IMMEDIATE")
        try:
            self._exec("INSERT INTO account(id,user_id) VALUES('self',%s)" % _quote(user_id))
            self._exec("COMMIT")
        except Exception:
            self._rollback()
            raise
        if self._scalar("SELECT count(*) FROM ps_crud") != "0":
            raise ValueError("Unexpected SQL outbox; retained unchanged")
        return True

    def reconcile_session(self, record):
        """Bind the validated durable pairing before exposing cached rows.

        A successful new code may belong to a different anonymous web user.
        The session file is saved first. If a reset interrupts the cache switch,
        startup repeats this transaction before reading any previous user's game.
        No pending move may be discarded to change accounts.
        """
        validate_session(record)
        user_id = record["user_id"]
        if user_id is None or record["refresh_token"] is None:
            raise ValueError("A paired session is required")
        rows = self._exec("SELECT user_id FROM account WHERE id='self'")
        if not rows or rows[0][0] == user_id:
            return self.bind_account(user_id)
        if record["pending_move"] is not None:
            raise ValueError("Saved move belongs to another account; retained")
        self._check_reserve()
        self.control("stop")
        self._exec("BEGIN IMMEDIATE")
        try:
            # Native clear(0) removes both public synced rows and their internal
            # oplog/buckets/checkpoints/subscriptions, preserving local tables.
            self._exec("SELECT powersync_clear(0)")
            self._exec("UPDATE account SET user_id=%s WHERE id='self'" % _quote(user_id))
            if self._scalar("SELECT count(*) FROM ps_crud") != "0":
                raise ValueError("Unexpected SQL outbox; retained unchanged")
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
