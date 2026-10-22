"""Host checks for pairing entry and the saved session. No badge and no network."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

APP = os.path.join(os.path.dirname(__file__), "..", "app", "dots_boxes")
sys.path.insert(0, APP)

from dots_pairing import CodeEntry
from dots_session import SessionFile, fresh_session, validate_session


class CodeEntryTests(unittest.TestCase):
    def test_fifth_letter_submits_and_up_deletes(self):
        entry = CodeEntry()
        entry.ready = True
        self.assertIsNone(entry.step(["A"]))
        self.assertEqual(entry.code, "A")
        entry.step([])
        self.assertIsNone(entry.step(["UP"]))
        self.assertEqual(entry.code, "")
        for letter in ("A", "B", "C", "A"):
            entry.step([])
            self.assertIsNone(entry.step([letter]))
        entry.step([])
        self.assertEqual(entry.step(["B"]), "ABCAB")

    def test_held_letter_does_not_repeat(self):
        entry = CodeEntry()
        entry.ready = True
        entry.step(["A"])
        self.assertEqual(entry.step(["A"]), None)
        self.assertEqual(entry.code, "A")


class SessionTests(unittest.TestCase):
    def paired(self):
        return dict(fresh_session("11111111-1111-4111-8111-111111111111"),
                    user_id="22222222-2222-4222-8222-222222222222",
                    room_id="33333333-3333-4333-8333-333333333333", seat=1,
                    refresh_token="refresh-" + "r" * 512,
                    access_token="access-" + "a" * 2048, expires_at=10000)

    def move(self):
        return {"id": "44444444-4444-4444-8444-444444444444",
                "orientation": "h", "row": 0, "col": 1}

    def test_round_trip(self):
        root = tempfile.mkdtemp()
        session = SessionFile(root)
        new_uuid = Mock(return_value="11111111-1111-4111-8111-111111111111")
        first = session.load(new_uuid)
        self.assertEqual(first["device_id"], new_uuid.return_value)
        self.assertIsNone(first["refresh_token"])
        again = session.load(new_uuid)
        self.assertEqual(again["device_id"], first["device_id"])
        paired = dict(first)
        paired.update(
            user_id="11111111-1111-4111-8111-111111111111",
            room_id="22222222-2222-4222-8222-222222222222",
            seat=1,
            refresh_token="refresh-token",
            access_token="access-token",
            expires_at=10,
            pending_move={"id": "33333333-3333-4333-8333-333333333333", "orientation": "h", "row": 0, "col": 1},
        )
        validate_session(paired)
        session.save(paired)
        self.assertEqual(session.load(new_uuid)["pending_move"]["col"], 1)
        new_uuid.assert_called_once_with()

    def test_rejects_a_different_shape(self):
        with self.assertRaises(ValueError):
            validate_session({"version": 1})
        with self.assertRaises(ValueError):
            fresh_session("not-a-uuid")

    def test_move_overlay_reuses_token_file_and_loads_pending_and_clear_after_restart(self):
        with tempfile.TemporaryDirectory() as root, patch("dots_session.os.sync"):
            saved = self.paired()
            session = SessionFile(root)
            session.save(saved)
            base = Path(session.path).read_bytes()
            pending = dict(saved, pending_move=self.move())
            session.save(pending)
            self.assertEqual(Path(session.path).read_bytes(), base)
            overlay = Path(session.pending_path).read_bytes()
            self.assertLess(len(overlay), len(base) // 10)
            self.assertNotIn(b"access_token", overlay)
            self.assertEqual(SessionFile(root).load(None), pending)
            session = SessionFile(root)
            session.load(None)
            session.save(saved)
            self.assertEqual(Path(session.path).read_bytes(), base)
            self.assertEqual(SessionFile(root).load(None), saved)
            self.assertIsNone(json.loads(Path(session.pending_path).read_text())["pending_move"])

    def test_legacy_session_uses_generation_zero_without_rewriting_saved_identity(self):
        with tempfile.TemporaryDirectory() as root, patch("dots_session.os.sync"):
            saved = self.paired()
            path = Path(root) / "session.json"
            original = json.dumps(saved).encode()
            path.write_bytes(original)
            session = SessionFile(root)
            self.assertEqual(session.load(None), saved)
            pending = dict(saved, pending_move=self.move())
            session.save(pending)
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(SessionFile(root).load(None), pending)
            self.assertEqual(json.loads(Path(session.pending_path).read_text())["generation"], 0)

    def test_base_generation_invalidates_old_overlay_after_token_or_account_change(self):
        for change in ({"access_token": "new-token"},
                       {"room_id": "55555555-5555-4555-8555-555555555555"},
                       {"user_id": "66666666-6666-4666-8666-666666666666"}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as root, patch("dots_session.os.sync"):
                session = SessionFile(root)
                saved = self.paired()
                session.save(saved)
                session.save(dict(saved, pending_move=self.move()))
                overlay = Path(session.pending_path).read_bytes()
                replacement = dict(saved, **change)
                session.save(replacement)
                self.assertEqual(Path(session.pending_path).read_bytes(), overlay)
                self.assertEqual(SessionFile(root).load(None), replacement)

    def test_noop_save_does_not_rewrite_or_flush(self):
        with tempfile.TemporaryDirectory() as root, patch("dots_session.os.sync") as sync:
            session = SessionFile(root)
            session.save(self.paired())
            sync.reset_mock()
            with patch("builtins.open", side_effect=AssertionError("no-op save must not write")):
                session.save(self.paired())
            sync.assert_not_called()

    def test_credential_refresh_keeps_pending_uuid_despite_stale_clear_overlay(self):
        with tempfile.TemporaryDirectory() as root, patch("dots_session.os.sync"):
            session = SessionFile(root)
            saved = self.paired()
            session.save(saved)
            session.save(dict(saved, pending_move=self.move()))
            session.save(saved)
            desired = dict(saved, access_token="refreshed-token", pending_move=self.move())
            session.save(desired)
            self.assertEqual(SessionFile(root).load(None), desired)

    def test_overlay_atomic_boundaries_recover_old_or_new_complete_move(self):
        for boundary in ("first_sync", "rename", "last_sync"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as root:
                saved = self.paired()
                session = SessionFile(root)
                with patch("dots_session.os.sync"):
                    session.save(saved)
                desired = dict(saved, pending_move=self.move())
                sync_failure = ([OSError("interrupted"), None] if boundary == "first_sync"
                                else [None, OSError("interrupted")] if boundary == "last_sync" else None)
                rename = os.rename
                with patch("dots_session.os.sync", side_effect=sync_failure), \
                        patch("dots_session.os.rename", side_effect=OSError("interrupted") if boundary == "rename" else rename):
                    with self.assertRaises(OSError):
                        session.save(desired)
                expected = desired if boundary == "last_sync" else saved
                self.assertEqual(SessionFile(root).load(None), expected)
                # A failed durability barrier must not make a retry a no-op.
                with patch("dots_session.os.sync") as sync:
                    session.save(desired)
                self.assertEqual(sync.call_count, 2)
                self.assertEqual(SessionFile(root).load(None), desired)

    def test_base_rename_interruption_never_applies_old_move_to_new_credentials(self):
        for boundary in ("first_sync", "rename", "last_sync"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory() as root:
                session = SessionFile(root)
                saved = dict(self.paired(), pending_move=self.move())
                with patch("dots_session.os.sync"):
                    session.save(self.paired())
                    session.save(saved)
                desired = dict(self.paired(), access_token="replacement-token")
                sync_failure = ([OSError("interrupted"), None] if boundary == "first_sync"
                                else [None, OSError("interrupted")] if boundary == "last_sync" else None)
                rename = os.rename
                with patch("dots_session.os.sync", side_effect=sync_failure), \
                        patch("dots_session.os.rename", side_effect=OSError("interrupted") if boundary == "rename" else rename):
                    with self.assertRaises(OSError):
                        session.save(desired)
                self.assertEqual(SessionFile(root).load(None), desired if boundary == "last_sync" else saved)
                with patch("dots_session.os.sync"):
                    session.save(desired)
                self.assertEqual(SessionFile(root).load(None), desired)

    def test_short_overlay_write_retains_saved_move_and_retry(self):
        with tempfile.TemporaryDirectory() as root, patch("dots_session.os.sync"):
            session = SessionFile(root)
            saved = self.paired()
            session.save(saved)
            target = Mock()
            target.__enter__ = Mock(return_value=target)
            target.__exit__ = Mock(return_value=False)
            target.write.return_value = 1
            with patch("builtins.open", return_value=target), self.assertRaisesRegex(OSError, "Incomplete"):
                session.save(dict(saved, pending_move=self.move()))
            self.assertEqual(SessionFile(root).load(None), saved)
            target.flush.assert_not_called()

    def test_orphan_or_invalid_overlay_is_retained_without_minting_an_identity(self):
        with tempfile.TemporaryDirectory() as root, patch("dots_session.os.sync"):
            session = SessionFile(root)
            session.save(self.paired())
            Path(session.pending_path).write_text('{"version":1,"generation":1,"pending_move":{}}')
            with self.assertRaisesRegex(ValueError, "Invalid pending"):
                SessionFile(root).load(None)
            Path(session.pending_path).write_text('{"version":1,"generation":2,"pending_move":null}')
            with self.assertRaisesRegex(ValueError, "newer session"):
                SessionFile(root).load(None)
            Path(session.path).unlink()
            new_uuid = Mock()
            with self.assertRaisesRegex(ValueError, "no session"):
                SessionFile(root).load(new_uuid)
            new_uuid.assert_not_called()


if __name__ == "__main__":
    unittest.main()
