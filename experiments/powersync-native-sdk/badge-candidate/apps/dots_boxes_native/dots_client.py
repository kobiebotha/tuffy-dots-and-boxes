"""Pair with a web room and render watched SDK query results."""
import time

from controls import Controller
from powersync.http import PENDING, Gateway
from dots_pairing import CodeEntry
from dots_session import fresh_session, validate_session
from dots_sync import Sync
from game import Game

PLAY_BUTTONS = (
    ("A", "left"),
    ("C", "right"),
    ("UP", "up"),
    ("DOWN", "down"),
    ("B", "b"),
)


def replay(game, moves):
    game.reset()
    for move in moves:
        if game.claim(move["orientation"], move["row"], move["col"]) is None:
            break


class Records:
    def __init__(self, session_file, record):
        self.session_file = session_file
        self.record = dict(record, pending_move=None)

    def save(self, value):
        credentials = dict(value, pending_move=None)
        self.session_file.save(credentials)
        self.record = credentials


class Client:
    LEAVE_HOLD_MS = 2000

    def __init__(self, store, session_file, heartbeat=None):
        from dots_config import (
            AUTH_URL, CA_FILES, MOVE_URL, PAIR_URL, POWERSYNC_URL, SUPABASE_ANON_KEY,
        )
        self.store = store
        self.heartbeat = heartbeat
        self.pair_url = PAIR_URL
        record = session_file.load(store.new_uuid)
        if record["user_id"]:
            # The store imports any legacy file-backed mutation once, before
            # the old files are discarded or the UI starts reading queries.
            store.reconcile_session(record)
        elif store.has_local_writes():
            raise ValueError("Unpaired credentials have unfinished SDK mutations")
        finish_migration = getattr(session_file, "discard_legacy_pending", None)
        if finish_migration is not None:
            finish_migration()
        self.records = Records(session_file, record)
        self.entry = CodeEntry()
        self.controller = Controller(Game())
        self.controller.on_claim = self._queue_move
        self.sync = None
        self.mode = "play" if self.records.record["refresh_token"] else "pair"
        self.notice = ""
        self.failure = None
        self._remote = None
        self._sending = None
        self._up_ms = 0
        self._leave_reason = None
        self._watch = None
        self._snapshot = None
        self._local_moves = 0
        self._room = None
        self._auth_url = AUTH_URL
        self._move_url = MOVE_URL
        self._endpoint = POWERSYNC_URL
        self._anon_key = SUPABASE_ANON_KEY
        self._ca_files = CA_FILES
        if self.mode == "play":
            self._open_sync()

    def _open_sync(self):
        self._close_watch()
        self._snapshot = None
        self._room = None
        self._local_moves = 0
        self.sync = Sync(
            self.store, self.records, self._endpoint, self._auth_url, self._move_url,
            self._anon_key, self._ca_files, self.heartbeat,
        )
        self._watch = self.store.watch(self.records.record["room_id"])

    def _close_watch(self):
        if self._watch is not None:
            self._watch.close()
            self._watch = None

    def _queue_move(self, preview):
        record = self.records.record
        if (self._leave_reason is not None or not self.controller.can_claim or self._local_moves
                or record["room_id"] is None or self.controller.game.edge_owner(*preview)):
            return False
        orientation, row, col = preview
        self.store.mutate_move(record["room_id"], orientation, row, col)
        # The SDK commit notification refreshes the watched query next frame.
        self.notice = ""
        return True

    def _apply_room(self):
        record = self.records.record
        snapshot = self._watch.read() if self._watch is not None else None
        changed = snapshot is not None
        if changed:
            self._snapshot = snapshot
            self._room = snapshot["room"]
            self._local_moves = snapshot["local_moves"]
        room = self._room
        game = self.controller.game
        self.controller.pending_edge = None
        self.controller.pending_player = 0
        if room is None:
            self.controller.can_claim = False
            if self.sync and self.sync.synced:
                self.controller.status_line = "ROOM UNAVAILABLE"
            else:
                self.controller.status_line = self.sync.status if self.sync else "SYNCING"
            return room
        if room["cols"] != game.cols or room["rows"] != game.rows:
            self.controller.game = Game(room["cols"], room["rows"])
            self.controller.col = room["cols"] // 2
            self.controller.row = room["rows"] // 2
            game = self.controller.game
            changed = True
        if changed:
            replay(game, self._snapshot["moves"])
        if not self._local_moves:
            game.scores[0] = room["score_p1"] or 0
            game.scores[1] = room["score_p2"] or 0
            if room["current_seat"] in (1, 2):
                game.current_player = room["current_seat"]
        self.controller.can_claim = (
            room["status"] == "playing" and game.current_player == record["seat"] and not self._local_moves
            and self.sync is not None and self.sync.synced and self.sync.phase == "stream"
        )
        if self.sync.phase == "revoked":
            line = "SIGN IN AGAIN"
        elif self.sync.phase == "blocked":
            line = self.sync.status
        elif self.sync.notice:
            line = self.sync.notice
        elif room["status"] == "waiting":
            line = "WAITING FOR BOTH BADGES"
        elif room["status"] == "cancelled":
            line = "ROOM CLOSED"
        elif self.sync.status != "SYNCED":
            line = self.sync.status
        else:
            line = ""
        self.controller.status_line = line
        return room

    def _leave_room(self):
        """Detach this badge only after its durable move has been resolved."""
        record = self.records.record
        self.controller.can_claim = False
        if self.store.has_local_writes() or self._local_moves:
            self.controller.status_line = "LOCAL MOVES MUST SYNC FIRST"
            return
        try:
            # A reset after this write must open pairing, never the old room.
            # Save before stopping sync or hiding the previous board.
            self.records.save(fresh_session(record["device_id"]))
        except (OSError, ValueError, RuntimeError, MemoryError):
            self.failure = "Could not leave. Reopen to retry."
            return
        try:
            self._close_watch()
            if self.sync is not None:
                self.sync.close()
                self.sync = None
        except (OSError, ValueError, RuntimeError, MemoryError):
            self.failure = "Room left. Reopen to finish."
            return
        # The inert cache stays bound to its old account. A different user's
        # successful pairing clears it before any board can be displayed.
        self.controller = Controller(Game())
        self.controller.on_claim = self._queue_move
        self.controller.can_claim = False
        self._room = self._snapshot = None
        self._local_moves = 0
        self.entry.clear()
        self.entry.status = self._leave_reason
        self.entry.ready = False
        self.entry.previous = set()
        self._up_ms = 0
        self._leave_reason = None
        self.notice = ""
        self.mode = "pair"

    def _pair_failed(self, code):
        if self._remote is not None:
            self._remote.close()
            self._remote = None
        self._sending = None
        self.entry.code = code[:-1] if code else ""
        self.entry.status = "TRY THAT CODE AGAIN"

    def _pair_step(self):
        if self._sending is None:
            return
        if self.store.has_local_writes() or self._local_moves:
            self._pair_failed(self._sending["code"])
            self.entry.status = "LOCAL MOVES MUST SYNC FIRST"
            return
        if self._remote is None:
            try:
                self._remote = Gateway(self.pair_url, self._ca_files, heartbeat=self.heartbeat,
                                       headers={"apikey": self._anon_key})
                self._remote.start(self._sending)
            except (OSError, ValueError):
                self._pair_failed(self._sending["code"])
                return
        try:
            result = self._remote.step()
        except (OSError, ValueError):
            self._pair_failed(self._sending["code"])
            return
        if result is PENDING:
            self.entry.status = "CHECKING CODE"
            return
        self._remote.close()
        self._remote = None
        code = self._sending["code"]
        self._sending = None
        status = result.get("http_status", 200 if result.get("access_token") else 503)
        if status == 401:
            self.entry.clear()
            self.entry.status = "UNKNOWN CODE"
            return
        if status == 409:
            self.entry.clear()
            self.entry.status = "ALREADY PAIRED"
            return
        if status >= 400 or "access_token" not in result:
            self.entry.code = code[:-1]
            self.entry.status = "TRY THAT CODE AGAIN"
            return
        try:
            expires_in = result.get("expires_in", 3600)
            if type(expires_in) is not int:
                expires_in = int(expires_in)
            updated = validate_session({
                "version": 1,
                "device_id": self.records.record["device_id"],
                "user_id": result["user_id"],
                "room_id": result["room_id"],
                "seat": int(result["seat"]),
                "refresh_token": result["refresh_token"],
                "access_token": result["access_token"],
                "expires_at": int(time.time()) + max(0, expires_in),
                "pending_move": None,
            })
            self._close_watch()
            if self.sync is not None:
                self.sync.close()
                self.sync = None
            # The durable session is authoritative across reset. Never clear
            # the old cache unless the replacement pairing has been saved.
            self.records.save(updated)
        except (OSError, ValueError, RuntimeError, MemoryError, KeyError, TypeError):
            self._pair_failed(code)
            return
        try:
            self.store.reconcile_session(self.records.record)
        except (OSError, ValueError, RuntimeError, MemoryError):
            # Startup will finish the idempotent transaction before any board
            # read. Keep the saved replacement credentials and fail closed.
            self.failure = "Pairing saved. Reopen to finish."
            self.controller.can_claim = False
            return
        self.entry.clear()
        self.controller = Controller(Game())
        self.controller.on_claim = self._queue_move
        self.controller.can_claim = False
        self.mode = "play"
        self._open_sync()

    def step(self, held, elapsed_ms):
        if self.failure:
            return
        held = list(held)
        if self.mode == "pair":
            if self._sending is None:
                finished = self.entry.step(held)
                if finished:
                    if len(self._anon_key) < 20:
                        self.entry.code = ""
                        self.entry.status = "APP KEY MISSING"
                    else:
                        self._sending = {"code": finished, "device_id": self.records.record["device_id"]}
            self._pair_step()
            return
        self.sync.step(elapsed_ms)
        if self.sync.notice:
            self.notice = self.sync.notice
        room = self._apply_room()
        if self.sync.phase == "revoked":
            self._leave_reason = "SIGN IN AGAIN"
        elif self.sync.synced and room is None:
            self._leave_reason = "ROOM UNAVAILABLE"
        elif room is not None and room["status"] == "cancelled":
            self._leave_reason = "ROOM CLOSED"
        if self._leave_reason is not None:
            self._leave_room()
            return
        if held == ["UP"] and not self.controller.wait_for_release:
            self._up_ms += max(0, elapsed_ms)
            if self._up_ms >= self.LEAVE_HOLD_MS:
                self._leave_reason = "LEFT ROOM"
                self._leave_room()
                return
        else:
            self._up_ms = 0
        names = [name for button, name in PLAY_BUTTONS if button in held]
        self.controller.step(names, elapsed_ms)

    def close(self):
        self._close_watch()
        if self._remote is not None:
            self._remote.close()
            self._remote = None
        if self.sync is not None:
            self.sync.close()
