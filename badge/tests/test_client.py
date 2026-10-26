"""Keep local edges visible, display frames cached and turns server-confirmed."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / path)
                for path in ("lib", "app/dots_boxes")]
from dots_client import Client
from dots_session import fresh_session, validate_session
from game import Game

ROOM = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"
DEVICE = "33333333-3333-4333-8333-333333333333"
OTHER_USER = "44444444-4444-4444-8444-444444444444"


class Session:
    def __init__(self):
        self.record = dict(fresh_session(DEVICE), room_id=ROOM, user_id=USER, seat=1,
                           access_token="token", refresh_token="refresh", expires_at=10**12)

    def load(self, _new_uuid):
        return self.record

    def save(self, record):
        self.record = record


class Store:
    def __init__(self):
        self.room_reads = self.move_reads = 0
        self.history = []
        self.read_after = []
        self.rows_read = 0
        self.value = {"status": "playing", "cols": 5, "rows": 4, "current_seat": 1,
                      "score_p1": 0, "score_p2": 0, "winner": None, "move_count": 0}

    def new_uuid(self):
        return "55555555-5555-4555-8555-555555555555"

    def reconcile_session(self, record):
        validate_session(record)
        pass

    def room(self, _room):
        self.room_reads += 1
        return self.value

    def moves(self, _room, after=0):
        self.move_reads += 1
        self.read_after.append(after)
        rows = [move for move in self.history if move["move_index"] > after]
        self.rows_read += len(rows)
        return rows


class Sync:
    def __init__(self):
        self.generation = 0
        self.phase = "stream"
        self.synced = True
        self.status = "SYNCED"
        self.notice = ""
        self.closed = False

    def step(self, _elapsed):
        pass

    def close(self):
        self.closed = True


class PairReply:
    def step(self):
        return {"user_id": OTHER_USER, "room_id": ROOM, "seat": 2,
                "refresh_token": "new-refresh", "access_token": "new-access", "expires_in": 3600}

    def close(self):
        pass


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.client = Client(self.store, Session())
        self.client.sync = Sync()

    def test_display_frames_read_only_once_until_a_committed_checkpoint(self):
        for _ in range(180):
            self.client.step([], 16)
        self.assertEqual((self.store.room_reads, self.store.move_reads), (1, 1))
        self.client.sync.generation += 1
        self.client.step([], 16)
        self.assertEqual((self.store.room_reads, self.store.move_reads), (2, 1))

    def publish(self, edges):
        self.store.history = [dict(orientation=edge[0], row=edge[1], col=edge[2], move_index=index + 1)
                              for index, edge in enumerate(edges)]
        canonical = Game()
        for edge in edges:
            canonical.claim(*edge)
        self.store.value.update(move_count=canonical.moves, score_p1=canonical.scores[0],
                                score_p2=canonical.scores[1], current_seat=canonical.current_player)
        self.client.sync.generation += 1
        self.client.step([], 16)
        self.assertEqual(self.client.controller.game.horizontal, canonical.horizontal)
        self.assertEqual(self.client.controller.game.vertical, canonical.vertical)
        self.assertEqual(self.client.controller.game.boxes, canonical.boxes)
        self.assertEqual(self.client.controller.game.scores, canonical.scores)
        return canonical

    def test_live_board_appends_only_new_moves_and_keeps_board_buffers(self):
        self.client.step([], 16)
        game = self.client.controller.game
        buffers = (game.horizontal, game.vertical, game.boxes)
        edges = ([('h', row, col) for row in range(5) for col in range(5)]
                 + [('v', row, col) for row in range(4) for col in range(6)])
        for count in range(1, 50):
            canonical = self.publish(edges[:count])
            self.assertEqual(game.moves, count)
            self.assertEqual(game.current_player, canonical.current_player)
            self.assertIs(game.horizontal, buffers[0])
            self.assertIs(game.vertical, buffers[1])
            self.assertIs(game.boxes, buffers[2])
        self.assertEqual(self.store.read_after, [0] + list(range(49)))
        self.assertEqual(self.store.rows_read, 49)
        self.assertEqual(game.scores, [10, 10])

    def test_catch_up_appends_several_moves_in_one_checkpoint(self):
        self.client.step([], 16)
        self.publish([('h', 0, 0)])
        self.publish([('h', 0, 0), ('h', 0, 1), ('h', 0, 2)])
        self.assertEqual(self.store.read_after, [0, 0, 1])
        self.assertEqual(self.client.controller.game.moves, 3)

    def test_cache_regression_rebuilds_the_canonical_board(self):
        self.publish([('h', 0, 0), ('v', 0, 0)])
        previous = self.client.controller.game.horizontal
        self.publish([('h', 0, 0)])
        self.assertIsNot(self.client.controller.game.horizontal, previous)
        self.assertEqual(self.store.read_after, [0, 0])

    def test_noncontiguous_suffix_falls_back_to_full_replay(self):
        self.publish([('h', 0, 0)])
        original = self.store.moves
        def missing_suffix(room, after=0):
            rows = original(room, after)
            return rows[1:] if after else rows
        self.store.moves = missing_suffix
        self.publish([('h', 0, 0), ('h', 0, 1), ('h', 0, 2)])
        self.assertEqual(self.store.read_after, [0, 1, 0])
        self.assertEqual(self.client.controller.game.moves, 3)

    def test_partial_bad_suffix_rebuilds_after_an_occupied_edge(self):
        self.publish([('h', 0, 0)])
        original = self.store.moves
        def occupied_suffix(room, after=0):
            rows = original(room, after)
            if after:
                rows[-1] = dict(rows[-1], orientation='h', row=0, col=0)
            return rows
        self.store.moves = occupied_suffix
        self.publish([('h', 0, 0), ('h', 0, 1), ('h', 0, 2)])
        self.assertEqual(self.store.read_after, [0, 1, 0])
        self.assertEqual(self.client.controller.game.moves, 3)

    def test_cached_turn_cannot_claim_before_first_or_covering_sync(self):
        self.client.sync.synced = False
        self.client.step([], 16)
        self.assertFalse(self.client.controller.can_claim)
        self.assertFalse(self.client._queue_move(("h", 0, 0)))
        self.client.sync.synced = True
        self.client.step([], 16)
        self.assertTrue(self.client.controller.can_claim)
        self.assertTrue(self.client._queue_move(("h", 0, 0)))
        self.client.records.save(dict(self.client.records.record, pending_move=None))
        self.client.sync.synced = False
        self.client.step([], 16)
        self.assertFalse(self.client.controller.can_claim)

    def test_network_update_to_taken_preview_does_not_send_a_duplicate(self):
        self.client.step([], 16)
        controller = self.client.controller
        controller.selecting = True
        controller.preview = ("h", 0, 0)
        controller.game.claim("h", 0, 0)
        controller.tap()
        self.assertIsNone(self.client.records.record["pending_move"])

    def test_claim_shows_saved_edge_on_the_same_button_release(self):
        self.client.step([], 16)
        controller = self.client.controller
        controller.selecting = True
        controller.preview = ("h", 0, 0)
        self.client.step(["B"], 16)
        self.client.step([], 16)
        self.assertFalse(controller.selecting)
        self.assertIsNone(controller.preview)
        self.assertEqual(controller.pending_edge, ("h", 0, 0))
        self.assertEqual(controller.pending_player, 1)
        self.assertFalse(controller.can_claim)
        self.assertEqual(controller.status_line, "SENDING MOVE")
        self.assertIsNotNone(self.client.records.record["pending_move"])
        # The local display must not invent canonical moves, scores or turns.
        self.assertEqual(controller.game.moves, 0)
        self.assertEqual(controller.game.current_player, 1)
        self.assertEqual(controller.game.scores, [0, 0])

    def test_pending_edge_stays_visible_offline_without_extra_sql_reads(self):
        self.client.step([], 16)
        self.client._queue_move(("v", 0, 0))
        self.client.sync.synced = False
        self.client.sync.phase = "token"
        self.client.sync.status = "OFFLINE"
        for _ in range(180):
            self.client.step([], 16)
        self.assertEqual(self.client.controller.pending_edge, ("v", 0, 0))
        self.assertFalse(self.client.controller.can_claim)
        self.assertEqual((self.store.room_reads, self.store.move_reads), (1, 1))

    def test_saved_pending_edge_reappears_on_reopen_before_sync(self):
        session = Session()
        session.save(dict(session.record, seat=2, pending_move={
            "id": DEVICE, "orientation": "h", "row": 0, "col": 0,
        }))
        client = Client(self.store, session)
        client.sync = Sync()
        client.sync.synced = False
        client.sync.phase = "token"
        client.step([], 16)
        self.assertEqual(client.controller.pending_edge, ("h", 0, 0))
        self.assertEqual(client.controller.pending_player, 2)
        self.assertFalse(client.controller.can_claim)

    def test_rejected_edge_disappears_without_board_or_generation_change(self):
        self.client.step([], 16)
        self.client._queue_move(("h", 0, 0))
        self.client.records.save(dict(self.client.records.record, pending_move=None))
        self.client.sync.synced = False
        self.client.sync.notice = "Move rejected"
        self.client.step([], 16)
        self.assertIsNone(self.client.controller.pending_edge)
        self.assertFalse(self.client.controller.can_claim)
        self.assertEqual(self.client.controller.status_line, "Move rejected")
        self.assertEqual(self.client.controller.game.edge_owner("h", 0, 0), 0)
        self.assertEqual((self.store.room_reads, self.store.move_reads), (1, 1))

    def test_failed_save_keeps_preview_without_showing_a_claimed_edge(self):
        self.client.step([], 16)
        controller = self.client.controller
        controller.selecting = True
        controller.preview = ("h", 0, 0)
        with patch.object(self.client.records.session_file, "save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                controller.tap()
        self.assertTrue(controller.selecting)
        self.assertEqual(controller.preview, ("h", 0, 0))
        self.assertIsNone(controller.pending_edge)
        self.assertIsNone(self.client.records.record["pending_move"])

    def test_closed_deleted_and_revoked_rooms_automatically_detach(self):
        for reason in ("closed", "deleted", "revoked"):
            with self.subTest(reason=reason):
                self.setUp()
                sync = self.client.sync
                if reason == "deleted":
                    self.store.value = None
                elif reason == "revoked":
                    self.client.sync.phase = "revoked"
                    self.client.sync.synced = False
                else:
                    self.store.value["status"] = "cancelled"
                self.client.step([], 16)
                self.assertEqual(self.client.mode, "pair")
                self.assertTrue(sync.closed)
                self.assertEqual(self.client.records.record, fresh_session(DEVICE))
                self.assertFalse(self.client.controller.can_claim)
                self.assertIsNone(self.client.sync)
                reads = (self.store.room_reads, self.store.move_reads)
                for _ in range(180):
                    self.client.step([], 16)
                self.assertEqual((self.store.room_reads, self.store.move_reads), reads)

    def test_missing_room_before_full_sync_keeps_saved_pairing(self):
        self.store.value = None
        self.client.sync.synced = False
        for _ in range(180):
            self.client.step([], 16)
        self.assertEqual(self.client.mode, "play")
        self.assertFalse(self.client.sync.closed)
        self.assertEqual(self.client.records.record["room_id"], ROOM)
        self.assertEqual((self.store.room_reads, self.store.move_reads), (1, 0))

    def test_short_up_preserves_cursor_movement_and_does_not_leave(self):
        self.client.step([], 16)
        row = self.client.controller.row
        self.client.step(["UP"], 16)
        self.client.step([], 16)
        self.assertEqual(self.client.controller.row, row - 1)
        self.assertEqual(self.client.mode, "play")
        self.assertEqual(self.client.records.record["room_id"], ROOM)

    def test_up_held_two_seconds_detaches_during_play_or_after_finish(self):
        for status in ("playing", "finished"):
            with self.subTest(status=status):
                self.setUp()
                self.store.value["status"] = status
                self.client.step([], 16)
                self.client.step(["UP"], 1999)
                self.assertEqual(self.client.mode, "play")
                self.client.step(["UP"], 1)
                self.assertEqual(self.client.mode, "pair")
                self.assertEqual(self.client.entry.status, "LEFT ROOM")
                self.assertEqual(self.client.records.record, fresh_session(DEVICE))

    def test_up_hold_requires_release_after_launch_and_ignores_combinations(self):
        self.client.step(["UP"], 2500)
        self.assertEqual(self.client.mode, "play")
        self.client.step([], 16)
        self.client.step(["UP", "A"], 2500)
        self.assertEqual(self.client.mode, "play")
        self.client.step(["UP"], 1500)
        self.client.step([], 16)
        self.client.step(["UP"], 1500)
        self.assertEqual(self.client.mode, "play")

    def test_held_up_does_not_reach_pairing_until_every_button_is_released(self):
        self.client.step([], 16)
        self.client.step(["UP"], 2000)
        self.client.step(["UP"], 2000)
        self.client.step(["A"], 16)
        self.assertEqual(self.client.entry.code, "")
        self.client.step([], 16)
        self.client.step(["A"], 16)
        self.assertEqual(self.client.entry.code, "A")

    def test_leave_saves_clean_session_before_closing_sync(self):
        self.client.step([], 16)
        events = []
        original_save = self.client.records.session_file.save

        def save(record):
            events.append("save")
            original_save(record)

        def close():
            self.assertEqual(self.client.records.session_file.record, fresh_session(DEVICE))
            events.append("close")

        with patch.object(self.client.records.session_file, "save", side_effect=save), \
                patch.object(self.client.sync, "close", side_effect=close), \
                patch.object(self.store, "reconcile_session") as reconcile:
            self.client.step(["UP"], 2000)
        self.assertEqual(events, ["save", "close"])
        reconcile.assert_not_called()
        self.assertEqual(self.client.controller.game.moves, 0)
        reopened = Client(self.store, self.client.records.session_file)
        self.assertEqual(reopened.mode, "pair")
        self.assertIsNone(reopened.sync)

    def test_failed_leave_save_keeps_session_cache_and_sync_intact(self):
        self.client.step([], 16)
        original = dict(self.client.records.record)
        sync = self.client.sync
        with patch.object(self.client.records.session_file, "save", side_effect=OSError("disk full")), \
                patch.object(self.store, "reconcile_session") as reconcile:
            self.client.step(["UP"], 2000)
        self.assertEqual(self.client.records.record, original)
        self.assertEqual(self.client.records.session_file.record, original)
        self.assertFalse(sync.closed)
        reconcile.assert_not_called()
        self.assertEqual(self.client.failure, "Could not leave. Reopen to retry.")

    def test_manual_leave_and_archive_wait_for_durable_move_resolution(self):
        for archived in (False, True):
            with self.subTest(archived=archived):
                self.setUp()
                self.client.step([], 16)
                self.client._queue_move(("h", 0, 0))
                pending = self.client.records.record["pending_move"]
                sync = self.client.sync
                if archived:
                    self.store.value["status"] = "cancelled"
                    sync.generation += 1
                self.client.step(["UP"] if not archived else [], 2000)
                self.assertEqual(self.client.mode, "play")
                self.assertEqual(self.client.records.record["pending_move"], pending)
                self.assertFalse(sync.closed)
                self.assertFalse(self.client.controller.can_claim)
                self.assertEqual(self.client.controller.status_line, "SAVED MOVE MUST SYNC FIRST")
                self.assertFalse(self.client._queue_move(("v", 0, 0)))
                self.client.records.save(dict(self.client.records.record, pending_move=None))
                self.client.step([], 16)
                self.assertEqual(self.client.mode, "pair")
                self.assertTrue(sync.closed)
                self.assertEqual(self.client.records.record, fresh_session(DEVICE))

    def test_revoked_session_never_discards_an_unconfirmed_move(self):
        self.client.step([], 16)
        self.client._queue_move(("h", 0, 0))
        original = dict(self.client.records.record)
        self.client.sync.phase = "revoked"
        self.client.sync.synced = False
        for _ in range(180):
            self.client.step([], 16)
        self.assertEqual(self.client.records.record, original)
        self.assertEqual(self.client.mode, "play")
        self.assertFalse(self.client.sync.closed)
        self.assertEqual(self.client.controller.status_line, "SAVED MOVE MUST SYNC FIRST")

    def ready_pairing(self):
        self.client.mode = "pair"
        self.client._sending = {"code": "ABCAB", "device_id": DEVICE}
        self.client._remote = PairReply()

    def test_new_account_pairing_saves_session_before_reconciling_cache(self):
        self.ready_pairing()
        self.client.controller.game.claim("h", 0, 0)
        events = []
        original_save = self.client.records.session_file.save

        def save(record):
            events.append("save")
            original_save(record)

        def reconcile(record):
            events.append("reconcile")
            self.assertEqual(self.client.records.session_file.record, record)

        with patch.object(self.client.sync, "close", side_effect=lambda: events.append("close")), \
                patch.object(self.client.records.session_file, "save", side_effect=save), \
                patch.object(self.store, "reconcile_session", side_effect=reconcile):
            self.client._pair_step()
        self.assertEqual(events, ["close", "save", "reconcile"])
        self.assertEqual(self.client.mode, "play")
        self.assertEqual(self.client.records.record["user_id"], OTHER_USER)
        self.assertEqual(self.client.controller.game.moves, 0)
        self.assertFalse(self.client.controller.can_claim)

    def test_failed_session_save_never_clears_old_cache(self):
        self.ready_pairing()
        old_record = dict(self.client.records.record)
        with patch.object(self.client.records.session_file, "save", side_effect=OSError("disk full")), \
                patch.object(self.store, "reconcile_session") as reconcile:
            self.client._pair_step()
        reconcile.assert_not_called()
        self.assertEqual(self.client.records.record, old_record)
        self.assertIsNone(self.client.failure)

    def test_failed_clear_keeps_saved_pairing_and_blocks_cached_board(self):
        self.ready_pairing()
        with patch.object(self.store, "reconcile_session", side_effect=OSError("interrupted")):
            self.client._pair_step()
        self.assertEqual(self.client.records.session_file.record["user_id"], OTHER_USER)
        self.assertEqual(self.client.failure, "Pairing saved. Reopen to finish.")
        self.assertFalse(self.client.controller.can_claim)
        with patch.object(self.store, "room") as read:
            self.client.step([], 16)
        read.assert_not_called()

    def test_new_code_never_reaches_server_with_a_saved_pending_move(self):
        self.ready_pairing()
        self.client._remote = None
        pending = {"id": DEVICE, "orientation": "h", "row": 0, "col": 0}
        self.client.records.save(dict(self.client.records.record, pending_move=pending))
        with patch("dots_client.Gateway") as gateway, \
                patch.object(self.store, "reconcile_session") as reconcile:
            self.client._pair_step()
        gateway.assert_not_called()
        reconcile.assert_not_called()
        self.assertEqual(self.client.records.record["pending_move"], pending)
        self.assertEqual(self.client.entry.status, "SAVED MOVE MUST SYNC FIRST")


if __name__ == "__main__":
    unittest.main()
