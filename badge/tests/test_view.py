"""Exercise pending-move drawing without the badge's graphics module."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app/dots_boxes"))
from controls import Controller
from view import View


class Screen:
    def __init__(self):
        self.pen = None
        self.calls = []

    def clear(self):
        self.calls.append(("clear", self.pen))

    def rectangle(self, x, y, width, height):
        self.calls.append(("rect", x, y, width, height, self.pen))

    def circle(self, x, y, radius):
        self.calls.append(("circle", x, y, radius, self.pen))

    def text(self, value, x, y, scale):
        self.calls.append(("text", value, x, y, scale, self.pen))

    def measure_text(self, value, scale):
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


if __name__ == "__main__":
    unittest.main()
