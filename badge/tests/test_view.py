"""Exercise pending-move drawing without the badge's graphics module."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / path)
                for path in ("lib", "app/dots_boxes")]
from controls import Controller
from view import View


class Screen:
    def __init__(self):
        self.pen = None
        self.calls = []
        self.measurements = []

    def clear(self):
        self.calls.append(("clear", self.pen))

    def rectangle(self, x, y, width, height):
        self.calls.append(("rect", x, y, width, height, self.pen))

    def circle(self, x, y, radius):
        self.calls.append(("circle", x, y, radius, self.pen))

    def text(self, value, x, y, scale):
        self.calls.append(("text", value, x, y, scale, self.pen))

    def measure_text(self, value, scale):
        self.measurements.append((value, scale))
        return len(value) * 6 * scale, 8 * scale


class Color:
    @staticmethod
    def rgb(red, green, blue):
        return red, green, blue


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.screen = Screen()
        self.view = View(self.screen, Color(), SimpleNamespace(sins="font"))
        self.controller = Controller()

    def board_edges(self):
        return [call for call in self.screen.calls if call[0] == "rect"
                and (call[3], call[4]) in ((34, 1), (1, 34), (34, 4), (4, 34))]

    def test_pending_edge_uses_its_players_color_and_claimed_thickness(self):
        for player in (1, 2):
            for orientation in ("h", "v"):
                with self.subTest(player=player, orientation=orientation):
                    self.setUp()
                    self.controller.game.current_player = 3 - player
                    self.controller.pending_edge = (orientation, 0, 0)
                    self.controller.pending_player = player
                    self.view.draw(self.controller)
                    geometry = (75, 44, 34, 4) if orientation == "h" else (73, 46, 4, 34)
                    pending = ("rect",) + geometry + (self.view.players[player],)
                    self.assertEqual(self.board_edges()[-1], pending)
                    self.assertEqual(len(self.board_edges()), 50)
                    first_dot = next(index for index, call in enumerate(self.screen.calls)
                                     if call[0] == "circle")
                    self.assertLess(self.screen.calls.index(pending), first_dot)
                    self.assertEqual(self.controller.game.edge_owner(orientation, 0, 0), 0)
                    self.assertEqual(self.controller.game.moves, 0)

    def test_canonical_owner_is_never_overwritten_by_a_pending_edge(self):
        self.controller.game.current_player = 2
        self.controller.game.claim("h", 0, 0)
        self.controller.pending_edge = ("h", 0, 0)
        self.controller.pending_player = 1
        self.view.draw(self.controller)
        self.assertEqual(len(self.board_edges()), 49)
        self.assertIn(("rect", 75, 44, 34, 4, self.view.players[2]), self.board_edges())
        self.assertNotIn(("rect", 75, 44, 34, 4, self.view.players[1]), self.board_edges())

    def test_no_pending_move_draws_the_ordinary_grid(self):
        self.assertIsNone(self.controller.pending_edge)
        self.assertEqual(self.controller.pending_player, 0)
        self.view.draw(self.controller)
        self.assertEqual(len(self.board_edges()), 49)
        self.assertTrue(all(call[-1] == self.view.guide for call in self.board_edges()))
        self.assertTrue(all(4 not in call[3:5] for call in self.board_edges()))
        ordinary = list(self.screen.calls)
        self.screen.calls = []
        self.controller.pending_player = 2
        self.view.draw(self.controller)
        self.assertEqual(self.screen.calls, ordinary)

    def test_only_the_local_seat_gets_the_me_label(self):
        for seat in (1, 2):
            with self.subTest(seat=seat):
                self.screen.calls = []
                self.view.draw(self.controller, local_seat=seat)
                labels = [call[1] for call in self.screen.calls if call[0] == "text"]
                self.assertIn("P%d (ME)" % seat, labels)
                self.assertIn("P%d" % (3 - seat), labels)
                self.assertNotIn("P%d (ME)" % (3 - seat), labels)

    def test_unpaired_view_does_not_identify_either_player_as_me(self):
        self.view.draw(self.controller)
        labels = [call[1] for call in self.screen.calls if call[0] == "text"]
        self.assertIn("P1", labels)
        self.assertIn("P2", labels)
        self.assertFalse(any("(ME)" in label for label in labels))

    def test_leave_hint_stays_visible_with_move_status_or_finished_board(self):
        for status in ("", "SENDING MOVE", "WAITING FOR BOTH BADGES"):
            with self.subTest(status=status):
                self.screen.calls = []
                self.controller.status_line = status
                self.view.draw(self.controller, local_seat=1)
                text = [call for call in self.screen.calls if call[0] == "text"]
                hint = next(call for call in text if call[1] == "HOLD UP 2s: LEAVE ROOM")
                self.assertEqual(hint[3], 230)
                self.assertLessEqual(hint[3] + self.screen.measure_text(hint[1], hint[4])[1], 240)
                if status:
                    self.assertIn(status, [call[1] for call in text])

    def test_local_labels_and_two_digit_scores_fit_their_panels(self):
        self.controller.game.scores = [20, 20]
        for seat in (1, 2):
            with self.subTest(seat=seat):
                self.screen.calls = []
                self.view.draw(self.controller, local_seat=seat)
                text = [call for call in self.screen.calls if call[0] == "text"]
                panels = [call for call in self.screen.calls
                          if call[0] == "rect" and call[-1] == self.view.panel]
                self.assertEqual(len(panels), 2)
                for player, panel in enumerate(panels, 1):
                    _, x, y, width, height, _ = panel
                    label = next(call for call in text
                                 if call[1] == ("P%d (ME)" % player if seat == player
                                                else "P%d" % player))
                    score = next(call for call in text
                                 if call[1] == "20" and x <= call[2] < x + width)
                    label_width, label_height = self.screen.measure_text(label[1], label[4])
                    score_width, score_height = self.screen.measure_text(score[1], score[4])
                    self.assertGreaterEqual(label[2], x)
                    self.assertGreaterEqual(label[3], y)
                    self.assertLessEqual(label[3] + label_height, y + height)
                    self.assertLess(label[2] + label_width, score[2])
                    self.assertGreaterEqual(score[3], y)
                    self.assertLessEqual(score[2] + score_width, x + width)
                    self.assertLessEqual(score[3] + score_height, y + height)

    def test_captured_boxes_keep_numeric_player_labels(self):
        self.controller.game.boxes[0] = 1
        self.controller.game.boxes[1] = 2
        for seat in (1, 2):
            with self.subTest(seat=seat):
                self.screen.calls = []
                self.view.draw(self.controller, local_seat=seat)
                box_labels = [call[1] for call in self.screen.calls
                              if call[0] == "text" and self.view.TOP <= call[3] < 192]
                self.assertEqual(box_labels, ["1", "2"])

    def test_idle_frames_skip_all_drawing_and_text_measurement(self):
        self.assertTrue(self.view.draw_if_changed(self.controller, local_seat=1))
        self.screen.calls = []
        measurements = len(self.screen.measurements)
        for remaining in range(1700, 1000, -10):
            self.controller.message_ms = remaining
            self.assertFalse(self.view.draw_if_changed(self.controller, local_seat=1))
        self.assertEqual(self.screen.calls, [])
        self.assertEqual(len(self.screen.measurements), measurements)

    def test_visible_state_and_in_place_board_changes_redraw(self):
        mutations = (
            lambda c: setattr(c, "col", c.col + 1),
            lambda c: setattr(c, "row", c.row + 1),
            lambda c: setattr(c, "selecting", True),
            lambda c: setattr(c, "pending_edge", ("h", 0, 0)),
            lambda c: setattr(c, "pending_player", 2),
            lambda c: setattr(c, "status_line", "SENDING MOVE"),
            lambda c: setattr(c, "can_claim", False),
            lambda c: setattr(c, "message", "A different message"),
            lambda c: setattr(c, "message_ms", 0),
            lambda c: setattr(c.game, "current_player", 2),
            lambda c: c.game.scores.__setitem__(0, 1),
            lambda c: c.game.horizontal.__setitem__(0, 1),
            lambda c: c.game.vertical.__setitem__(0, 2),
            lambda c: c.game.boxes.__setitem__(0, 1),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.setUp()
                self.controller.pending_edge = ("h", 1, 0)
                self.view.draw_if_changed(self.controller)
                self.screen.calls = []
                mutation(self.controller)
                self.assertTrue(self.view.draw_if_changed(self.controller))
                self.assertTrue(self.screen.calls)
                self.assertFalse(self.view.draw_if_changed(self.controller))

    def test_confirmation_replaces_pending_edge_and_reset_same_count_redraws(self):
        self.controller.pending_edge = ("h", 0, 0)
        self.controller.pending_player = 1
        self.view.draw_if_changed(self.controller)
        self.controller.game.claim("h", 0, 0)
        self.controller.pending_edge = None
        self.controller.pending_player = 0
        self.screen.calls = []
        self.assertTrue(self.view.draw_if_changed(self.controller))
        self.assertEqual(len(self.board_edges()), 49)
        self.assertIn(("rect", 75, 44, 34, 4, self.view.players[1]), self.board_edges())
        self.controller.game.reset()
        self.controller.game.claim("v", 0, 0)
        self.assertTrue(self.view.draw_if_changed(self.controller))

    def test_only_flash_phase_and_expiration_require_redraw(self):
        self.controller.game.boxes[0] = 1
        self.controller.flash_boxes = ((0, 0),)
        self.controller.flash_ms = 899
        self.view.draw_if_changed(self.controller)
        self.controller.flash_ms = 850
        self.assertFalse(self.view.draw_if_changed(self.controller))
        self.controller.flash_ms = 749
        self.assertTrue(self.view.draw_if_changed(self.controller))
        self.controller.flash_ms = 0
        self.assertTrue(self.view.draw_if_changed(self.controller))

    def test_pairing_messages_and_returning_to_game_invalidate_the_scene(self):
        self.assertTrue(self.view.draw_if_changed(self.controller))
        self.assertTrue(self.view.pairing_if_changed("AB", "ENTER CODE"))
        self.assertFalse(self.view.pairing_if_changed("AB", "ENTER CODE"))
        self.assertTrue(self.view.pairing_if_changed("ABC", "ENTER CODE"))
        self.assertTrue(self.view.message_if_changed("Error", "Press Home"))
        self.assertFalse(self.view.message_if_changed("Error", "Press Home"))
        self.assertTrue(self.view.draw_if_changed(self.controller))
        self.view.message("Forced", "New screen")
        self.assertTrue(self.view.draw_if_changed(self.controller))

    def test_text_cache_is_bounded_and_reused_for_cursor_frames(self):
        self.view.draw(self.controller)
        measurements = len(self.screen.measurements)
        self.controller.col += 1
        self.view.draw(self.controller)
        self.assertEqual(len(self.screen.measurements), measurements)
        for index in range(100):
            self.view.measure("Changing error %d" % index)
        self.assertLessEqual(len(self.view._measurements), 64)
        measurements = len(self.screen.measurements)
        self.view.measure("Changing error 99")
        self.assertEqual(len(self.screen.measurements), measurements)


class RetainedLoopTests(unittest.TestCase):
    def run_app(self, outcome="normal"):
        screen = Screen()
        events = []
        original_clear = object()
        badge = SimpleNamespace(default_clear=original_clear, mode=lambda _: None,
                                held=lambda _: False)
        home = SimpleNamespace(irq=lambda **_: None, value=lambda: True)
        machine = SimpleNamespace(
            Pin=SimpleNamespace(board=SimpleNamespace(BUTTON_HOME=home), IRQ_FALLING=1),
            WDT=lambda **_: SimpleNamespace(feed=lambda: None))

        class Store:
            root = "/state/test"

            def __init__(self, **_):
                pass

            def open(self):
                pass

            def close(self):
                events.append(("store_close", badge.default_clear))

        class Client:
            def __init__(self, *_args, **_kwargs):
                self.failure = "Open failed" if outcome == "failure" else None
                self.mode = "play"
                self.controller = Controller()
                self.records = SimpleNamespace(record={"seat": 1})

            def step(self, *_args):
                pass

            def close(self):
                events.append(("client_close", badge.default_clear))

        namespace = {"badge": badge, "HIRES": 1, "VSYNC": 2, "screen": screen,
                     "color": Color(), "font": SimpleNamespace(sins="font")}
        namespace.update({name: index for index, name in enumerate(
            ("BUTTON_A", "BUTTON_B", "BUTTON_C", "BUTTON_UP", "BUTTON_DOWN"))})

        def run(update):
            self.assertIsNone(badge.default_clear)
            if outcome == "home":
                namespace["request_exit"]()
                self.assertIs(update(), True)
                return
            self.assertIsNone(update())
            count = len(screen.calls)
            # Model Badgeware's optional clear: default_clear=None retains pixels.
            if badge.default_clear is not None:
                screen.clear()
            self.assertIsNone(update())
            self.assertEqual(len(screen.calls), count)
            if outcome == "raise":
                raise RuntimeError("loop failed")

        namespace["run"] = run
        modules = {
            "badgeware": SimpleNamespace(display=SimpleNamespace(update=lambda: None),
                                         reset=lambda: events.append(("reset", badge.default_clear))),
            "machine": machine,
            "time": SimpleNamespace(ticks_ms=lambda: 1, ticks_diff=lambda a, b: a - b,
                                    sleep_ms=lambda _: None),
            "dots_client": SimpleNamespace(Client=Client),
            "dots_storage": SimpleNamespace(Store=Store),
            "dots_session": SimpleNamespace(SessionFile=lambda _: None),
        }
        app = Path(__file__).resolve().parents[1] / "app/dots_boxes/__init__.py"
        with patch.dict(sys.modules, modules):
            if outcome == "raise":
                with self.assertRaisesRegex(RuntimeError, "loop failed"):
                    exec(compile(app.read_text(), str(app), "exec"), namespace)
            else:
                exec(compile(app.read_text(), str(app), "exec"), namespace)
        self.assertIs(badge.default_clear, original_clear)
        self.assertEqual(events[:2], [("client_close", original_clear),
                                      ("store_close", original_clear)])
        return screen, events

    def test_managed_loop_retains_idle_game_and_failure_frames(self):
        self.run_app()
        self.run_app("failure")

    def test_clear_setting_and_cleanup_survive_loop_exception(self):
        self.run_app("raise")

    def test_home_restores_setting_and_forces_the_final_message(self):
        screen, events = self.run_app("home")
        self.assertEqual(events[-1][0], "reset")
        self.assertIn("Returning home", [call[1] for call in screen.calls if call[0] == "text"])


if __name__ == "__main__":
    unittest.main()
