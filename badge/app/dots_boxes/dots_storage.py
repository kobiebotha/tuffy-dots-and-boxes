"""Bounded game database on Badgeware's LittleFS mount.

Synced rows arrive only through PowerSync. The local account row records which
web user this file belongs to. A leftover journal or unknown file is retained.
"""
import json
import os
import time

from dots_session import FILES as SESSION_FILES
from dots_session import is_uuid, validate_session
from dots_codec import COLS, ROWS, decode_edge, decode_status, encode_uuid

ROOT = "/state/dots_boxes"
OWNER = "dots-boxes-app-local-v1\n"
DATABASE = "state.sqlite"
# Shared 1MB LittleFS. A badge that already has Badgegotchi had about 248KB
# free, so the old 512KB cap could not be created. One room fits in 128KB.
MAX_DATABASE_BYTES = 128 * 1024
MIN_FREE_BYTES = 64 * 1024
PAGE_BYTES = 1024
MAX_SYNC_PAGES = 96
MAX_SYNC_LINE_BYTES = 8192
MAX_SYNC_BATCH_RECORDS = 6
MAX_BUCKETS = 32
MAX_MOVES = 64
QUERY_ROWS = 16  # The qualified native wrapper rejects results over 32 rows.
SQLITE_CACHE_PAGES = 32
_ZERO_BYTES = bytes(1024)
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
    "s INTEGER,p INTEGER,a INTEGER,b INTEGER,w INTEGER,n INTEGER)",
    "CREATE TABLE moves (id TEXT PRIMARY KEY NOT NULL, r TEXT,e INTEGER,n INTEGER)",
)
SYNCED_TYPES = ("rooms", "moves")
ALLOWED_NAMES = ("owner.txt", DATABASE, DATABASE + "-journal") + SESSION_FILES


def _stat(path):
    try:
        return os.stat(path)
    except OSError as exc:
        if exc.args[0] != 2:
            raise
        return None


def _quote(value):
    return "'" + value.replace("'", "''") + "'"


def _free_bytes(root):
    stat = os.statvfs(root)
    return stat[1] * stat[4]


def _number(value):
    if value is None:
        return None
    return int(value)


class BadgeVFS:
    """One connection with a bounded database shadow and a physical journal.

    SQLite's FULL journal barriers still precede every database sync. Between
    those barriers, random database I/O uses PSRAM; xSync writes its complete
    logical image sequentially and durably flushes it. This avoids LittleFS
    copying a file's tail at every random seek. The journal is never buffered.
    """

    def __init__(self, root, heartbeat=None):
        self.paths = (root + "/" + DATABASE, root + "/" + DATABASE + "-journal")
        self.files = {}
        self.next_handle = 1
        self.heartbeat = heartbeat
        self.last_error = None
        self.db_buffer = bytearray(MAX_DATABASE_BYTES)
        self.db_size = 0
        self.db_dirty = False
        self.db_handle = None

    def _path(self, path):
        if path not in self.paths:
            raise ValueError("Storage path is not owned by this game")
        return path

    def _zero_db(self, start, stop):
        while start < stop:
            end = min(stop, start + len(_ZERO_BYTES))
            self.db_buffer[start:end] = _ZERO_BYTES[:end - start]
            start = end

    def _load_db(self, stream, path, flags):
        size = os.stat(path)[6]
        if size > MAX_DATABASE_BYTES:
            raise ValueError("Database exceeds storage limit")
        if size and stream.readinto(memoryview(self.db_buffer)[:size]) != size:
            raise OSError("Could not read the complete database")
        if not flags & 1:
            # LittleFS marks read-write opens dirty for their mtime attribute.
            # statvfs traverses that open CTZ and its directory entry separately,
            # counting this unchanged file twice until file_sync clears DIRTY.
            # Commit only that metadata now, before checking snapshot reserve.
            stream.flush()
        self.db_size, self.db_dirty = size, False

    def _sync_db(self, stream):
        if self.db_dirty:
            stream.seek(0)
            if stream.write(memoryview(self.db_buffer)[:self.db_size]) != self.db_size:
                raise OSError("Incomplete database write; journal retained")
        # LittleFS flush is file_sync, including its data and directory commit.
        # Retain the qualified barrier and keep dirty data until both succeed.
        stream.flush()
        os.sync()
        self.db_dirty = False

    def __call__(self, op, item, arg1, arg2):
        try:
            if self.heartbeat:
                self.heartbeat()
            if op == 9:
                if not 0 <= arg1 <= 512:
                    raise ValueError("Invalid entropy request")
                return (0, os.urandom(arg1))
            if op == 10:
                seconds = int(time.time())
                if time.gmtime(0)[0] == 2000:
                    seconds += 946684800
                elif time.gmtime(0)[0] != 1970:
                    raise ValueError("Unsupported clock epoch")
                return (0, (seconds // 86400, seconds % 86400))
            if op == 0:
                path = self._path(item)
                if any(entry[1] == path for entry in self.files.values()):
                    return (5, 0)
                flags = arg1
                exists = _stat(path) is not None
                if (not exists and not flags & 4) or (exists and flags & 16):
                    return (14, 0)
                mode = "rb" if flags & 1 else ("r+b" if exists else "x+b")
                stream = open(path, mode)
                if path == self.paths[0]:
                    try:
                        self._load_db(stream, path, flags)
                    except Exception:
                        stream.close()
                        raise
                handle = self.next_handle
                self.next_handle += 1
                self.files[handle] = (stream, path, flags)
                if path == self.paths[0]:
                    self.db_handle = handle
                return (0, handle)
            if op == 7:
                path = self._path(item)
                if path != self.paths[1]:
                    raise ValueError("Database deletion is not supported")
                if _stat(path) is not None:
                    os.remove(path)
                if arg1:
                    os.sync()
                return (0, 0)
            if op == 8:
                return (0, int(_stat(self._path(item)) is not None))
            stream, path, flags = self.files[item]
            is_db = item == self.db_handle
            if op == 1:
                if is_db and self.db_dirty:
                    self._sync_db(stream)
                stream.close()
                del self.files[item]
                if is_db:
                    self.db_handle = None
                    self.db_size = 0
                if flags & 8:
                    if path != self.paths[1]:
                        raise ValueError("Database deletion is not supported")
                    os.remove(path)
                return (0, 0)
            if op == 2:
                if is_db:
                    if arg1 < 0 or arg2 < 0 or arg2 > MAX_DATABASE_BYTES:
                        return (10, 0)
                    end = min(self.db_size, arg1 + arg2)
                    return (0, bytes(memoryview(self.db_buffer)[arg1:end]))
                stream.seek(arg1)
                return (0, stream.read(arg2))
            if op == 3:
                if is_db:
                    end = arg1 + len(arg2)
                    if flags & 1:
                        return (10, 0)
                    if arg1 < 0 or end > MAX_DATABASE_BYTES:
                        return (13, 0)
                    if arg2:
                        self._zero_db(self.db_size, arg1)
                        self.db_buffer[arg1:end] = arg2
                        self.db_size = max(self.db_size, end)
                        self.db_dirty = True
                    return (0, len(arg2))
                if arg1 < 0 or arg1 + len(arg2) > MAX_DATABASE_BYTES + 4096:
                    return (13, 0)
                stream.seek(arg1)
                return (0, stream.write(arg2))
            if op == 4:
                if is_db:
                    if flags & 1:
                        return (10, 0)
                    if arg1 < self.db_size:
                        self.last_error = "File truncation is unavailable"
                        return (1546, 0)
                    if arg1 > MAX_DATABASE_BYTES:
                        return (13, 0)
                    if arg1 > self.db_size:
                        self._zero_db(self.db_size, arg1)
                        self.db_size, self.db_dirty = arg1, True
                    return (0, 0)
                stream.seek(0, 2)
                size = stream.tell()
                if arg1 < size:
                    self.last_error = "File truncation is unavailable"
                    return (1546, 0)
                if arg1 > MAX_DATABASE_BYTES:
                    return (13, 0)
                if arg1 > size:
                    stream.seek(arg1 - 1)
                    if stream.write(b"\0") != 1:
                        return (10, 0)
                return (0, 0)
            if op == 5:
                if is_db:
                    self._sync_db(stream)
                    return (0, 0)
                stream.flush()
                os.sync()
                return (0, 0)
            if op == 6:
                if is_db:
                    return (0, self.db_size)
                position = stream.tell()
                stream.seek(0, 2)
                size = stream.tell()
                stream.seek(position)
                return (0, size)
            return (10, 0)
        except Exception as exc:
            self.last_error = type(exc).__name__ + ": " + str(exc)
            return (14 if op == 0 else 10, 0)

    def close_all(self):
        for handle in list(self.files):
            if self(1, handle, 0, 0)[0]:
                raise OSError("Could not close game storage")


class Store:
    def __init__(self, heartbeat=None, native=None, root=ROOT):
        if not root.startswith("/") or root.endswith("/") or "/../" in root:
            raise ValueError("Storage needs an absolute owned directory")
        self.root = root
        self.heartbeat = heartbeat
        self.native = native
        self.io = None
        self.opened = False
        # Alternate schemas own their native DDL; the application creates only
        # its exact schema. This also keeps bounded host projection probes usable.
        self.schema = SCHEMA
        self.sync_schema = {
            "tables": [], "raw_tables": json.loads(self.schema).get("raw_tables", []),
        }

    def _feed(self):
        if self.heartbeat:
            self.heartbeat()

    def _exec(self, sql):
        self._feed()
        result = self.native.exec(sql)
        self._feed()
        return result

    def _scalar(self, sql):
        rows = self._exec(sql)
        if len(rows) != 1 or len(rows[0]) != 1:
            raise ValueError("Unexpected database result")
        return rows[0][0]

    def _owned_directory(self):
        marker = self.root + "/owner.txt"
        if _stat(self.root) is None:
            parent = self.root.rsplit("/", 1)[0] or "/"
            if _free_bytes(parent) < MAX_DATABASE_BYTES + MIN_FREE_BYTES:
                raise OSError("Not enough free badge storage")
            os.mkdir(self.root)
            with open(marker, "x") as target:
                target.write(OWNER)
                target.flush()
            os.sync()
        else:
            if _stat(marker) is None:
                raise ValueError("Game directory has no owner marker")
            with open(marker) as source:
                if source.read(128) != OWNER:
                    raise ValueError("Game directory belongs to another app")
        for name in os.listdir(self.root):
            info = os.stat(self.root + "/" + name)
            if name not in ALLOWED_NAMES or info[0] & 0xf000 != 0x8000:
                raise ValueError("Unrecognized game data; retained unchanged")
        journal = _stat(self.root + "/" + DATABASE + "-journal")
        if journal and journal[6]:
            raise ValueError("Interrupted write; database and journal retained")

    def open(self):
        if self.opened:
            return self
        if self.native is None:
            from dots_runtime import load_native
            self.native = load_native(self.heartbeat)
        self._owned_directory()
        path = self.root + "/" + DATABASE
        before = _stat(path)
        if before and (before[6] == 0 or before[6] > MAX_DATABASE_BYTES):
            raise ValueError("Unrecognized database size; retained unchanged")
        self.io = BadgeVFS(self.root, self.heartbeat)
        self.native.configure(self.io)
        try:
            self.native.open(path)
            self.opened = True
            if self._scalar("SELECT powersync_rs_version()") != "0.5.4/cratesio":
                raise ValueError("Unsupported PowerSync core")
            schema_version = self._scalar("PRAGMA user_version") if before else None
            if before and schema_version not in ("1", "2", "3", "4"):
                raise ValueError("Unsupported database schema; retained unchanged")
            if before and self._scalar("PRAGMA quick_check") != "ok":
                raise ValueError("Database integrity check failed; retained unchanged")
            if before and self._scalar("SELECT count(*) FROM ps_crud") != "0":
                raise ValueError("Unexpected SQL outbox; retained unchanged")
            self._exec("PRAGMA page_size=%d" % PAGE_BYTES)
            if self._scalar("PRAGMA page_size") != str(PAGE_BYTES):
                raise ValueError("Unsupported database page size")
            self._exec("PRAGMA max_page_count=%d" % (MAX_DATABASE_BYTES // PAGE_BYTES))
            self._exec("PRAGMA cache_size=8")
            self._exec("PRAGMA temp_store=MEMORY")
            self._exec("PRAGMA synchronous=FULL")
            if self._scalar("PRAGMA journal_mode=DELETE") != "delete":
                raise ValueError("DELETE journaling is required")
            migrating = not before or schema_version in ("1", "2", "3")
            if migrating:
                self._check_reserve()
            self._exec("BEGIN IMMEDIATE")
            try:
                if migrating and before:
                    # Re-download only synchronized cache. The local account,
                    # client id and separate durable pairing/move file survive.
                    # SQLite reuses the old file's pages; this VFS cannot shrink it.
                    self._exec("SELECT powersync_clear(0)")
                # The raw clear callbacks live on this connection, so register
                # them on every open before reconciliation can change accounts.
                # On migration this also drops the old rooms/moves JSON views.
                self._exec("SELECT powersync_replace_schema(%s)" % _quote(self.schema))
                if migrating:
                    if self.schema == APP_SCHEMA:
                        for statement in RAW_TABLE_SQL:
                            self._exec(statement)
                    self._exec("PRAGMA user_version=4")
                self._exec("COMMIT")
            except Exception:
                self._rollback()
                raise
            if self._scalar("SELECT count(*) FROM ps_crud") != "0":
                raise ValueError("Unexpected SQL outbox; retained unchanged")
            # Register and migrate with the smaller cache first; schema4 then
            # has room for 32 pages in the fixed 256KiB native arena.
            self._exec("PRAGMA cache_size=%d" % SQLITE_CACHE_PAGES)
            return self
        except Exception:
            self.close()
            raise

    def _rollback(self):
        try:
            self._exec("ROLLBACK")
        except Exception:
            pass

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

    def moves(self, room_id):
        if not is_uuid(room_id):
            raise ValueError("Invalid room")
        rows = []
        while True:
            page = self._exec(
                "SELECT e,n FROM moves WHERE r=%s "
                "ORDER BY n LIMIT %d OFFSET %d" % (_quote(encode_uuid(room_id)), QUERY_ROWS, len(rows))
            )
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

    def control(self, command, payload=None):
        allowed = (
            "start", "stop", "line_text", "seed_checkpoint_request_id",
            "update_subscriptions", "subscriptions", "connection", "refreshed_token",
            "next_checkpoint_request_id", "current_checkpoint_request_id",
            "target_checkpoint_request_id",
        )
        if command not in allowed:
            raise ValueError("Unsupported sync control")
        if payload is not None and not isinstance(payload, str):
            payload = json.dumps(payload)
        if payload is not None and len(payload.encode()) > MAX_SYNC_LINE_BYTES:
            raise ValueError("Sync response is too large")
        if command == "line_text":
            self._sync_line(payload)
            self._check_sync_budget()
        else:
            self._check_reserve()
        self._exec("BEGIN IMMEDIATE")
        try:
            result = self._apply_control(command, payload)
            self._exec("COMMIT")
        except Exception:
            self._rollback()
            raise
        return result

    def retire_subscriptions(self):
        """Remove this app's persisted TTLs before selecting its current room.

        A reset can interrupt re-pairing before the old Sync.close(). The core's
        unsubscribe key includes the exact JSON parameters, not only its name.
        """
        actions = self._retired_subscriptions()
        if actions:
            self.control_group(actions)

    def _retired_subscriptions(self):
        actions = []
        offset = 0
        while True:
            rows = self._exec(
                "SELECT stream_name,local_params FROM ps_stream_subscriptions "
                "WHERE ttl IS NOT NULL LIMIT %d OFFSET %d" % (QUERY_ROWS, offset)
            )
            if not rows:
                return actions
            for name, params in rows:
                payload = '{"unsubscribe":{"name":' + json.dumps(name) + ',"params":' + params + '}}'
                actions.append(("subscriptions", payload))
                if len(actions) > 60:
                    raise ValueError("Too many saved stream subscriptions")
            offset += len(rows)

    def control_group(self, actions):
        """Commit related native commands once and return results after commit."""
        if not isinstance(actions, list) or not 1 <= len(actions) <= 64:
            raise ValueError("Invalid sync control group")
        prepared = []
        size = 0
        allowed = ("subscriptions", "start", "seed_checkpoint_request_id",
                   "next_checkpoint_request_id", "target_checkpoint_request_id")
        for command, payload in actions:
            if command not in allowed:
                raise ValueError("Unsupported grouped sync control")
            if payload is not None and not isinstance(payload, str):
                payload = json.dumps(payload)
            size += len(payload.encode()) if payload is not None else 0
            if size > MAX_SYNC_LINE_BYTES:
                raise ValueError("Sync control group is too large")
            prepared.append((command, payload))
        results = []
        self._check_reserve()
        self._exec("BEGIN IMMEDIATE")
        try:
            for command, payload in prepared:
                results.append(self._apply_control(command, payload))
            self._exec("COMMIT")
        except Exception:
            self._rollback()
            raise
        return results

    def start_sync(self, stream, options, clear_target=False):
        actions = self._retired_subscriptions()
        actions.append(("subscriptions", {"subscribe": {"stream": stream, "ttl": 300}}))
        actions.append(("start", options))
        if clear_target:
            actions.append(("target_checkpoint_request_id", "0"))
        results = self.control_group(actions)
        return results[-2] if clear_target else results[-1]

    def seed_checkpoint(self, accepted):
        results = self.control_group([
            ("seed_checkpoint_request_id", accepted), ("next_checkpoint_request_id", None),
        ])
        return str(results[-1])

    def _sync_line(self, payload):
        line = json.loads(payload)
        if not isinstance(line, dict):
            raise ValueError("Invalid sync line")
        checkpoint = line.get("checkpoint")
        if "checkpoint" in line:
            if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("buckets", []), list):
                raise ValueError("Invalid sync checkpoint")
            if len(checkpoint.get("buckets", [])) > MAX_BUCKETS:
                raise ValueError("Unexpected sync buckets")
        data = line.get("data")
        if "data" in line:
            if not isinstance(data, dict) or not isinstance(data.get("data", []), list):
                raise ValueError("Invalid sync data")
            operations = data.get("data", [])
            if len(operations) > MAX_SYNC_BATCH_RECORDS:
                raise ValueError("Sync batch exceeds contract")
            for op in operations:
                if not isinstance(op, dict):
                    raise ValueError("Invalid sync operation")
                if op.get("op") in ("PUT", "REMOVE") and op.get("object_type") not in SYNCED_TYPES:
                    raise ValueError("Unexpected synced table")
        return line

    def _check_sync_budget(self):
        # clear(0) returns old cache pages to SQLite's freelist without shrinking
        # the file. Those reusable pages must not prevent a migrated badge from
        # downloading its replacement projection. The hard file cap still holds.
        if self._used_pages() > MAX_SYNC_PAGES:
            raise ValueError("Sync storage budget reached; retained data needs recovery")
        self._check_reserve()

    def _used_pages(self):
        return (int(self._scalar("PRAGMA page_count"))
                - int(self._scalar("PRAGMA freelist_count")))

    def _apply_control(self, command, payload):
        if command == "start":
            options = {} if payload is None else json.loads(payload)
            if not isinstance(options, dict):
                raise ValueError("Invalid sync start options")
            # Raw table routing is not inferred from SQLite's native tables.
            # Supply it on every start, including after a reset or reconnect.
            if "schema" not in options:
                options["schema"] = self.sync_schema
            payload = json.dumps(options)
            if len(payload.encode()) > MAX_SYNC_LINE_BYTES:
                raise ValueError("Sync start options are too large")
        argument = "NULL" if payload is None else _quote(payload)
        result = self._scalar("SELECT powersync_control(%s,%s)" % (_quote(command), argument))
        return json.loads(result) if result is not None else None

    def has_local_writes(self):
        return self._scalar("SELECT count(*) FROM ps_crud") != "0"

    def control_batch(self, payloads):
        """Compatibility entry point for batches containing only data records."""
        if not isinstance(payloads, list) or not 1 <= len(payloads) <= MAX_SYNC_BATCH_RECORDS:
            raise ValueError("Sync batch exceeds record limit")
        for payload in payloads:
            if not isinstance(payload, str):
                raise ValueError("Invalid sync batch record")
            if len(payload.encode()) > MAX_SYNC_LINE_BYTES:
                raise ValueError("Sync batch exceeds byte limit")
            line = self._sync_line(payload)
            if len(line) != 1 or not isinstance(line.get("data"), dict):
                raise ValueError("Only sync data can be batched")
        instructions = []
        for result in self.control_lines(payloads):
            instructions.extend(result["instructions"])
        return instructions

    def control_lines(self, payloads):
        """Commit ordered metadata and data records in one bounded transaction.

        Each operation's JSON and checksum reach the native core unchanged.
        Checkpoint completion stays attached to its own instruction result, so
        the caller distinguishes partial progress from a full checkpoint. No
        instructions escape until every record and the FULL commit succeed.
        """
        if not isinstance(payloads, list) or not 1 <= len(payloads) <= MAX_SYNC_BATCH_RECORDS:
            raise ValueError("Sync batch exceeds record limit")
        size = operations = 0
        for payload in payloads:
            if not isinstance(payload, str):
                raise ValueError("Invalid sync batch record")
            size += len(payload.encode())
            if size > MAX_SYNC_LINE_BYTES:
                raise ValueError("Sync batch exceeds byte limit")
            line = self._sync_line(payload)
            if len(line) != 1:
                raise ValueError("Invalid sync record envelope")
            operations += len(line.get("data", {}).get("data", []))
            if operations > MAX_SYNC_BATCH_RECORDS:
                raise ValueError("Sync batch exceeds operation limit")
        self._check_sync_budget()
        results = []
        self._exec("BEGIN IMMEDIATE")
        try:
            for payload in payloads:
                if self._used_pages() > MAX_SYNC_PAGES:
                    raise ValueError("Sync storage budget reached; retained data needs recovery")
                result = self._apply_control("line_text", payload)
                if not isinstance(result, list):
                    raise ValueError("Invalid core batch response")
                results.append({"instructions": result,
                                "full_checkpoint": "checkpoint_complete" in json.loads(payload)})
            self._exec("COMMIT")
        except Exception:
            self._rollback()
            raise
        return results

    def _check_reserve(self):
        # LittleFS keeps the previous CTZ file image until the sequential
        # replacement is durably committed. Reserve a second complete image
        # in addition to the remaining room for the journal and metadata.
        if self.io is not None and self.io.db_handle is not None:
            snapshot_bytes = self.io.db_size
        else:
            info = _stat(self.root + "/" + DATABASE)
            snapshot_bytes = info[6] if info is not None else 0
        if _free_bytes(self.root) < snapshot_bytes + MIN_FREE_BYTES:
            raise OSError("Storage is low; database snapshot needs more free space")

    def close(self):
        if self.opened:
            self._feed()
            self.native.close()
            self.opened = False
        if self.io:
            self.io.close_all()
            self.io = None
