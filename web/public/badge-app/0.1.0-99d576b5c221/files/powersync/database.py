"""Bounded SQLite lifecycle and PowerSync core controls, independent of app data."""
import json
import os

from sqlite_shadow_vfs import ShadowVFS

DATABASE = "state.sqlite"
MAX_DATABASE_BYTES = 128 * 1024
MIN_FREE_BYTES = 64 * 1024

PAGE_BYTES = 1024
MAX_SYNC_PAGES = 96
MAX_SYNC_LINE_BYTES = 8192
MAX_SYNC_BATCH_RECORDS = 6
MAX_BUCKETS = 32
QUERY_ROWS = 16
SQLITE_CACHE_PAGES = 32


def _stat(path):
    try:
        return os.stat(path)
    except OSError as exc:
        if exc.args[0] != 2:
            raise
        return None


def _free_bytes(root):
    stat = os.statvfs(root)
    return stat[1] * stat[4]


def _quote(value):
    return "'" + value.replace("'", "''") + "'"


class Database:
    """One bounded PowerSync database. Subclasses may override _initialize.

    schema is PowerSync schema JSON; create_statements initialize raw tables.
    _initialize runs inside the open transaction, before any rows are exposed.
    The backend adapter must implement configure(vfs), open(path), exec(sql),
    and close(). A future native SDK adapter can replace this implementation.
    """
    def __init__(self, schema, root, owner, allowed_files=(), synced_types=None,
                 heartbeat=None, native=None, schema_version=1, previous_versions=(),
                 create_statements=()):
        if (not root.startswith("/") or root.endswith("/")
                or any(part in (".", "..", "") for part in root.split("/")[1:])):
            raise ValueError("Storage needs an absolute owned directory")
        self.root, self.owner = root, owner
        self.allowed_names = ("owner.txt", DATABASE, DATABASE + "-journal") + tuple(allowed_files)
        self.schema_version = str(schema_version)
        self.accepted_versions = tuple(str(v) for v in previous_versions) + (self.schema_version,)
        self.create_statements = create_statements
        self.heartbeat = heartbeat
        self.native = native
        self.io = None
        self.opened = False
        self.schema = schema if isinstance(schema, str) else json.dumps(schema)
        decoded = json.loads(self.schema)
        self.synced_types = tuple(synced_types) if synced_types is not None else tuple(
            t["name"] for t in decoded.get("tables", []) + decoded.get("raw_tables", [])
            if not t.get("local_only"))
        self.sync_schema = {
            "tables": [t for t in decoded.get("tables", []) if not t.get("local_only")],
            "raw_tables": decoded.get("raw_tables", []),
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

    def execute(self, sql):
        """Run bounded SQL; rows contain text or None in the pinned adapter.

        Results must fit the native adapter's 32-row limit. Use paged queries
        for larger sets. App writes belong to the connector's durable queue.
        """
        return self._exec(sql)

    def scalar(self, sql):
        """Run a query that returns exactly one row and one column."""
        return self._scalar(sql)

    def new_uuid(self):
        return self._scalar("SELECT uuid()")

    def _owned_directory(self):
        marker = self.root + "/owner.txt"
        if _stat(self.root) is None:
            parent = self.root.rsplit("/", 1)[0] or "/"
            if _free_bytes(parent) < MAX_DATABASE_BYTES + MIN_FREE_BYTES:
                raise OSError("Not enough free badge storage")
            os.mkdir(self.root)
            with open(marker, "x") as target:
                target.write(self.owner)
                target.flush()
            os.sync()
        else:
            if _stat(marker) is None:
                raise ValueError("Database directory has no owner marker")
            with open(marker) as source:
                if source.read(128) != self.owner:
                    raise ValueError("Database directory belongs to another app")
        for name in os.listdir(self.root):
            info = os.stat(self.root + "/" + name)
            if name not in self.allowed_names or info[0] & 0xf000 != 0x8000:
                raise ValueError("Unrecognized application data; retained unchanged")
        journal = _stat(self.root + "/" + DATABASE + "-journal")
        if journal and journal[6]:
            raise ValueError("Interrupted write; database and journal retained")

    def open(self):
        if self.opened:
            return self
        if self.native is None:
            from .runtime import load_native
            self.native = load_native(self.heartbeat)
        self._owned_directory()
        path = self.root + "/" + DATABASE
        before = _stat(path)
        if before and (before[6] == 0 or before[6] > MAX_DATABASE_BYTES):
            raise ValueError("Unrecognized database size; retained unchanged")
        self.io = ShadowVFS(path, max_database_bytes=MAX_DATABASE_BYTES, heartbeat=self.heartbeat)
        self.native.configure(self.io)
        try:
            self.native.open(path)
            self.opened = True
            if self._scalar("SELECT powersync_rs_version()") != "0.5.4/cratesio":
                raise ValueError("Unsupported PowerSync core")
            schema_version = self._scalar("PRAGMA user_version") if before else None
            if before and schema_version not in self.accepted_versions:
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
            self._check_reserve()
            self._exec("BEGIN IMMEDIATE")
            try:
                self._initialize(schema_version)
                self._exec("COMMIT")
            except Exception:
                self._rollback()
                raise
            if self._scalar("SELECT count(*) FROM ps_crud") != "0":
                raise ValueError("Unexpected SQL outbox; retained unchanged")
            # Initialize with a small cache before using the fixed native arena.
            self._exec("PRAGMA cache_size=%d" % SQLITE_CACHE_PAGES)
            return self
        except Exception:
            self.close()
            raise

    def _initialize(self, version):
        if version is not None and version != self.schema_version:
            raise ValueError("A schema migration implementation is required")
        self._replace_schema()
        if version is None:
            for statement in self.create_statements:
                self._exec(statement)
            self._exec("PRAGMA user_version=" + self.schema_version)

    def _replace_schema(self):
        # Raw-table clear callbacks must be registered on every open.
        self._exec("SELECT powersync_replace_schema(%s)" % _quote(self.schema))

    def _rollback(self):
        try:
            self._exec("ROLLBACK")
        except Exception:
            pass

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
        """Remove persisted TTLs before selecting the current subscription.

        A reset can interrupt switching subscriptions before SyncClient.close().
        The unsubscribe key includes exact JSON parameters, not only its name.
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
        return self._validate_sync_line(json.loads(payload))

    def _validate_sync_line(self, line):
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
                if op.get("op") in ("PUT", "REMOVE") and op.get("object_type") not in self.synced_types:
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
        rows = self._exec("PRAGMA page_count; PRAGMA freelist_count")
        if len(rows) != 2 or len(rows[0]) != 1 or len(rows[1]) != 1:
            raise ValueError("Unexpected database page counts")
        return int(rows[0][0]) - int(rows[1][0])

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

    def control_lines(self, payloads, parsed=None):
        """Commit ordered metadata and data records in one bounded transaction.

        Each operation's JSON and checksum reach the native core unchanged.
        Checkpoint completion stays attached to its own instruction result, so
        the caller distinguishes partial progress from a full checkpoint. No
        instructions escape until every record and the FULL commit succeed.
        parsed may contain the matching records already decoded by StreamLines;
        it is an internal fast path for the same payloads, not independent input.
        """
        if not isinstance(payloads, list) or not 1 <= len(payloads) <= MAX_SYNC_BATCH_RECORDS:
            raise ValueError("Sync batch exceeds record limit")
        if parsed is not None and (not isinstance(parsed, list) or len(parsed) != len(payloads)):
            raise ValueError("Invalid decoded sync records")
        size = operations = 0
        decoded = []
        for index, payload in enumerate(payloads):
            if not isinstance(payload, str):
                raise ValueError("Invalid sync batch record")
            size += len(payload.encode())
            if size > MAX_SYNC_LINE_BYTES:
                raise ValueError("Sync batch exceeds byte limit")
            line = self._sync_line(payload) if parsed is None else self._validate_sync_line(parsed[index])
            if len(line) != 1:
                raise ValueError("Invalid sync record envelope")
            operations += len(line.get("data", {}).get("data", []))
            if operations > MAX_SYNC_BATCH_RECORDS:
                raise ValueError("Sync batch exceeds operation limit")
            decoded.append(line)
        self._check_reserve()
        results = []
        self._exec("BEGIN IMMEDIATE")
        try:
            for payload, line in zip(payloads, decoded):
                if self._used_pages() > MAX_SYNC_PAGES:
                    raise ValueError("Sync storage budget reached; retained data needs recovery")
                result = self._apply_control("line_text", payload)
                if not isinstance(result, list):
                    raise ValueError("Invalid core batch response")
                results.append({"instructions": result,
                                "full_checkpoint": "checkpoint_complete" in line})
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
