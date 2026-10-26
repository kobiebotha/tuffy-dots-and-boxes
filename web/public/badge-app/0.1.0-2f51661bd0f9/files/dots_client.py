"""Pair with a web room and show saved local edges while they sync."""
import time

from controls import Controller
from dots_http import PENDING, Gateway
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
        self.record = record

    def save(self, value):
        self.session_file.save(value)
        self.record = value


class Client:
    LEAVE_HOLD_MS = 2000

    def __init__(self, store, session_file, heartbeat=None):
        from dots_config import (
            AUTH_URL, CA_FILES, MOVE_URL, PAIR_URL, POWERSYNC_URL, SUPABASE_ANON_KEY,
        )
        self.store = store
        self.heartbeat = heartbeat
        self.pair_url = PAIR_URL
        self.records = Records(session_file, session_file.load(store.new_uuid))
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
        self._move_count = None
        self._room_key = None
        self._room = None
        self._auth_url = AUTH_URL
        self._move_url = MOVE_URL
        self._endpoint = POWERSYNC_URL
        self._anon_key = SUPABASE_ANON_KEY
        self._ca_files = CA_FILES
        if self.records.record["user_id"]:
            store.reconcile_session(self.records.record)
        if self.mode == "play":
            self._open_sync()

    def _open_sync(self):
        self._room_key = None
        self._room = None
        self._move_count = None
        self.sync = Sync(
            self.store, self.records, self._endpoint, self._auth_url, self._move_url,
            self._anon_key, self._ca_files, self.heartbeat,
        )

    def _queue_move(self, preview):
        record = self.records.record
        if (self._leave_reason is not None or not self.controller.can_claim or record["pending_move"] is not None
                or record["room_id"] is None or self.controller.game.edge_owner(*preview)):
            return False
        orientation, row, col = preview
        self.records.save(dict(record, pending_move={
            "id": self.store.new_uuid(),
            "orientation": orientation,
            "row": row,
            "col": col,
        }))
        # The controller clears its yellow preview on this same button release.
        # Publish the saved edge now, before the next network step can run.
        self._apply_room()
        self.notice = ""
        return True

    def _apply_room(self):
        record = self.records.record
        # The native core publishes rows atomically at DidCompleteSync. Reading
        # twice on every display frame only burns CPU and fragments the heap.
        key = (record["room_id"], self.sync.generation if self.sync else 0)
        if key != self._room_key:
            self._room = self.store.room(record["room_id"]) if record["room_id"] else None
            self._room_key = key
        room = self._room
        game = self.controller.game
        if room is None:
            self.controller.pending_edge = None
            self.controller.pending_player = 0
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
            self._move_count = None
        if room["move_count"] != self._move_count:
            self._update_moves(record["room_id"], room["move_count"])
            self._move_count = room["move_count"]
        game.scores[0] = room["score_p1"] or 0
        game.scores[1] = room["score_p2"] or 0
        if room["current_seat"] in (1, 2):
            game.current_player = room["current_seat"]
        move = record["pending_move"]
        pending = move is not None
        # This display layer survives retries/restarts with the durable session.
        # Scores, turn and boxes still come from the published canonical board.
        self.controller.pending_edge = (move["orientation"], move["row"], move["col"]) if pending else None
        self.controller.pending_player = record["seat"] if pending else 0
        self.controller.can_claim = (
            room["status"] == "playing" and room["current_seat"] == record["seat"] and not pending
            and self.sync is not None and self.sync.synced and self.sync.phase == "stream"
        )
        if pending:
            line = "SENDING MOVE"
        elif self.sync.phase == "revoked":
            line = "SIGN IN AGAIN"
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

    def _update_moves(self, room_id, count):
        """Append immutable published moves; rebuild after reset or reconciliation.

        A checkpoint can contain several moves. Require an unbroken suffix before
        using it, and retain the full replay path for a regressed or rebuilt cache.
        Pending edges are display overlays and never enter this canonical board.
        """
        game = self.controller.game
        previous = self._move_count
        if previous is not None and previous == game.moves and previous < count:
            moves = self.store.moves(room_id, after=previous)
            if (len(moves) == count - previous
                    and all(move["move_index"] == previous + index + 1
                            for index, move in enumerate(moves))):
                for move in moves:
                    if game.claim(move["orientation"], move["row"], move["col"]) is None:
                        break
                else:
                    return
        replay(game, self.store.moves(room_id))

    def _leave_room(self):
        """Detach this badge only after its durable move has been resolved."""
        record = self.records.record
        self.controller.can_claim = False
        if record["pending_move"] is not None:
            self.controller.status_line = "SAVED MOVE MUST SYNC FIRST"
            return
        try:
            # A reset after this write must open pairing, never the old room.
            # Save before stopping sync or hiding the previous board.
            self.records.save(fresh_session(record["device_id"]))
        except (OSError, ValueError, RuntimeError, MemoryError):
            self.failure = "Could not leave. Reopen to retry."
            return
        try:
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
        self._room = self._room_key = self._move_count = None
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
        if self.records.record["pending_move"] is not None:
            self._pair_failed(self._sending["code"])
            self.entry.status = "SAVED MOVE MUST SYNC FIRST"
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
        if self._remote is not None:
            self._remote.close()
            self._remote = None
        if self.sync is not None:
            self.sync.close()
