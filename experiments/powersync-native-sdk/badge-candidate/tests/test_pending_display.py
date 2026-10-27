"""Mutation/query UI flow: SDK rows are rendered without a pending journal."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

APP = Path(__file__).resolve().parents[1] / 'apps/dots_boxes_native'
sys.path.insert(0, str(APP))
from dots_client import Client, Records
from dots_session import SessionFile, fresh_session
from controls import Controller
from game import Game

ROOM = '550e8400-e29b-41d4-a716-446655440000'
MOVE = '550e8400-e29b-41d4-a716-446655440001'
USER = '550e8400-e29b-41d4-a716-446655440002'
DEVICE = '550e8400-e29b-41d4-a716-446655440003'


def credentials():
    return dict(fresh_session(DEVICE), room_id=ROOM, user_id=USER, seat=1,
                access_token='fixture-access', refresh_token='fixture-refresh', expires_at=3600)


def room():
    return {'cols': 5, 'rows': 4, 'status': 'playing', 'current_seat': 1,
            'score_p1': 0, 'score_p2': 0, 'move_count': 0, 'winner': None}


def snapshot(local=True):
    return {'room': room(), 'moves': [{'orientation': 'h', 'row': 1, 'col': 3}] if local else [],
            'local_moves': int(local)}


class QueryDisplayTests(unittest.TestCase):
    def client(self):
        client = Client.__new__(Client)
        client.records = SimpleNamespace(record=credentials(), save=Mock())
        client.controller = Controller(Game())
        client.controller.can_claim = True
        client._leave_reason = None
        client._local_moves = 0
        client._snapshot = client._room = None
        client._watch = SimpleNamespace(read=lambda: None, invalidate=Mock())
        client.sync = SimpleNamespace(generation=0, synced=True, phase='stream', status='SYNCED', notice='')
        client.notice = ''
        return client

    def test_claim_only_mutates_sdk_table_and_waits_for_sdk_watch(self):
        client = self.client()
        calls = []
        client.store = SimpleNamespace(mutate_move=lambda *args: calls.append(('mutation', args)) or MOVE)
        client._watch.invalidate = lambda: calls.append(('invalidate',))
        client._apply_room = lambda: calls.append(('query',))
        self.assertTrue(client._queue_move(('h', 1, 3)))
        self.assertEqual(calls, [('mutation', (ROOM, 'h', 1, 3))])
        client.records.save.assert_not_called()
        self.assertIsNone(client.records.record['pending_move'])

    def test_failed_sdk_mutation_does_not_write_session_or_invent_display_state(self):
        client = self.client()
        client.store = SimpleNamespace(mutate_move=Mock(side_effect=OSError('Flash full')))
        previous = snapshot(False)
        client._snapshot = previous
        with self.assertRaisesRegex(OSError, 'Flash full'):
            client._queue_move(('h', 1, 3))
        self.assertIs(client._snapshot, previous)
        self.assertEqual(client._local_moves, 0)
        client.records.save.assert_not_called()
        client._watch.invalidate.assert_not_called()

    def test_watched_optimistic_row_is_rendered_as_an_ordinary_game_edge(self):
        client = self.client()
        values = [snapshot(True), None]
        client._watch.read = lambda: values.pop(0)
        client._apply_room()
        self.assertEqual(client.controller.game.edge_owner('h', 1, 3), 1)
        self.assertEqual(client.controller.game.moves, 1)
        self.assertEqual(client.controller.game.current_player, 2)
        self.assertIsNone(client.controller.pending_edge)
        self.assertEqual(client.controller.pending_player, 0)
        self.assertFalse(client.controller.can_claim)
        client._apply_room()
        self.assertEqual(client.controller.game.moves, 1)
        client.records.save.assert_not_called()

    def test_rejected_mutation_disappears_when_sdk_query_reverts(self):
        client = self.client()
        values = [snapshot(True), snapshot(False)]
        client._watch.read = lambda: values.pop(0)
        client._apply_room()
        self.assertEqual(client.controller.game.moves, 1)
        client.sync.notice = 'Move rejected'
        client._apply_room()
        self.assertEqual(client.controller.game.moves, 0)
        self.assertFalse(client.controller.game.edge_owner('h', 1, 3))
        self.assertEqual(client.controller.status_line, 'Move rejected')
        self.assertTrue(client.controller.can_claim)
        client.records.save.assert_not_called()

    def test_retry_and_stopped_status_do_not_need_a_pending_record(self):
        client = self.client()
        client._watch.read = lambda: snapshot(True)
        for phase, status, expected in (
            ('upload', 'SENDING', 'SENDING'),
            ('retry', 'OFFLINE', 'OFFLINE'),
            ('blocked', 'SYNC ERROR', 'SYNC ERROR'),
            ('revoked', 'SIGN IN AGAIN', 'SIGN IN AGAIN'),
        ):
            with self.subTest(phase=phase):
                client.sync.phase, client.sync.status = phase, status
                client._apply_room()
                self.assertEqual(client.controller.status_line, expected)
                self.assertEqual(client.controller.game.moves, 1)
                self.assertIsNone(client.records.record['pending_move'])
                self.assertIsNone(client.controller.pending_edge)


class CredentialSessionTests(unittest.TestCase):
    def test_new_session_writes_credentials_without_pending_journal(self):
        with tempfile.TemporaryDirectory() as root:
            session = SessionFile(root)
            value = credentials()
            session.save(value)
            before = Path(session.path).read_bytes()
            session.save(dict(value, pending_move={'id': MOVE, 'orientation': 'h', 'row': 0, 'col': 0}))
            self.assertEqual(Path(session.path).read_bytes(), before)
            self.assertFalse(Path(session.pending_path).exists())
            self.assertIsNone(session.load(None)['pending_move'])

    def test_legacy_pending_is_read_once_then_removed_after_database_adoption(self):
        with tempfile.TemporaryDirectory() as root:
            session = SessionFile(root)
            value = credentials()
            Path(session.path).write_text(json.dumps({'format': 2, 'generation': 4, 'session': value}))
            legacy = {'id': MOVE, 'orientation': 'h', 'row': 0, 'col': 0}
            Path(session.pending_path).write_text(json.dumps({'version': 1, 'generation': 4, 'pending_move': legacy}))
            before = Path(session.path).read_bytes()
            self.assertEqual(session.load(None)['pending_move'], legacy)
            self.assertTrue(Path(session.pending_path).exists())
            session.discard_legacy_pending()
            self.assertFalse(Path(session.pending_path).exists())
            self.assertEqual(Path(session.path).read_bytes(), before)
            self.assertIsNone(session.load(None)['pending_move'])
            # Completion is idempotent and does not rewrite credentials again.
            session.discard_legacy_pending()
            self.assertEqual(Path(session.path).read_bytes(), before)

    def test_records_never_pass_mutations_to_credential_file(self):
        session = SimpleNamespace(save=Mock())
        records = Records(session, credentials())
        value = dict(credentials(), pending_move={'id': MOVE, 'orientation': 'h', 'row': 0, 'col': 0})
        records.save(value)
        self.assertIsNone(session.save.call_args.args[0]['pending_move'])
        self.assertIsNone(records.record['pending_move'])


if __name__ == '__main__':
    unittest.main()
