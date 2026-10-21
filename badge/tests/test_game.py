import random
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app' / 'dots_boxes'))
from game import Game


def edges_for(game):
    return ([('h', row, col)
             for row in range(game.rows + 1)
             for col in range(game.cols)]
            + [('v', row, col)
               for row in range(game.rows)
               for col in range(game.cols + 1)])


def snapshot(game):
    return (bytes(game.horizontal), bytes(game.vertical), bytes(game.boxes),
            tuple(game.scores), game.current_player, game.moves,
            game.finished, game.winner)


class GameTests(unittest.TestCase):
    def test_new_default_game(self):
        game = Game()
        self.assertEqual((game.cols, game.rows), (5, 4))
        self.assertEqual(len(game.horizontal), 25)
        self.assertEqual(len(game.vertical), 24)
        self.assertEqual(len(game.boxes), 20)
        self.assertEqual(game.scores, [0, 0])
        self.assertEqual(game.current_player, 1)
        self.assertEqual(game.moves, 0)
        self.assertFalse(game.finished)
        self.assertIsNone(game.winner)
        self.assertFalse(any(game.horizontal + game.vertical + game.boxes))

    def test_invalid_board_dimensions(self):
        for cols, rows in [(0, 1), (1, 0), (-1, 2), (2, -1),
                           (1.5, 2), (2, 1.5), ('5', 4), (5, None)]:
            with self.subTest(cols=cols, rows=rows):
                with self.assertRaises(ValueError):
                    Game(cols, rows)

    def test_non_scoring_moves_alternate_and_record_owner(self):
        game = Game()
        self.assertEqual(game.claim('h', 0, 0), ())
        self.assertEqual(game.edge_owner('h', 0, 0), 1)
        self.assertEqual(game.current_player, 2)
        self.assertEqual(game.claim('v', 0, 0), ())
        self.assertEqual(game.edge_owner('v', 0, 0), 2)
        self.assertEqual(game.current_player, 1)
        self.assertEqual(game.moves, 2)
        self.assertEqual(game.scores, [0, 0])

    def test_duplicate_is_a_no_op(self):
        game = Game()
        game.claim('h', 0, 0)
        before = snapshot(game)
        self.assertIsNone(game.claim('h', 0, 0))
        self.assertEqual(snapshot(game), before)

    def test_each_edge_can_be_addressed_including_all_boundaries(self):
        game = Game(3, 2)
        for edge in edges_for(game):
            with self.subTest(edge=edge):
                game.reset()
                self.assertEqual(game.edge_owner(*edge), 0)
                self.assertEqual(game.claim(*edge), ())
                self.assertEqual(game.edge_owner(*edge), 1)
                self.assertEqual(sum(game.horizontal + game.vertical), 1)

    def test_invalid_edges_raise_without_changing_game(self):
        game = Game(3, 2)
        invalid = [('h', -1, 0), ('h', 3, 0), ('h', 0, -1), ('h', 0, 3),
                   ('v', -1, 0), ('v', 2, 0), ('v', 0, -1), ('v', 0, 4),
                   ('x', 0, 0), ('H', 0, 0), (None, 0, 0),
                   ('h', 0.5, 0), ('v', 0, '1'), ('h', None, 0)]
        before = snapshot(game)
        for edge in invalid:
            with self.subTest(edge=edge):
                with self.assertRaises(ValueError):
                    game.edge_owner(*edge)
                with self.assertRaises(ValueError):
                    game.claim(*edge)
                self.assertEqual(snapshot(game), before)

    def test_every_box_can_be_completed_from_each_of_its_four_sides(self):
        for row in range(2):
            for col in range(3):
                surrounding = [('h', row, col), ('h', row + 1, col),
                               ('v', row, col), ('v', row, col + 1)]
                for final_edge in surrounding:
                    with self.subTest(box=(row, col), final=final_edge):
                        game = Game(3, 2)
                        for edge in surrounding:
                            if edge != final_edge:
                                self.assertEqual(game.claim(*edge), ())
                        scoring_player = game.current_player
                        self.assertEqual(game.claim(*final_edge), ((row, col),))
                        self.assertEqual(game.boxes[row * 3 + col], scoring_player)
                        self.assertEqual(game.scores[scoring_player - 1], 1)
                        self.assertEqual(game.current_player, scoring_player)
                        self.assertEqual(sum(bool(box) for box in game.boxes), 1)

    def test_vertical_shared_edge_awards_two_boxes(self):
        game = Game(2, 1)
        for edge in [('h', 0, 0), ('h', 0, 1), ('h', 1, 0),
                     ('h', 1, 1), ('v', 0, 0), ('v', 0, 2)]:
            self.assertEqual(game.claim(*edge), ())
        self.assertEqual(game.claim('v', 0, 1), ((0, 0), (0, 1)))
        self.assertEqual(list(game.boxes), [1, 1])
        self.assertEqual(game.scores, [2, 0])
        self.assertEqual(game.current_player, 1)
        self.assertTrue(game.finished)
        self.assertEqual(game.winner, 1)

    def test_horizontal_shared_edge_awards_two_boxes(self):
        game = Game(1, 2)
        for edge in [('h', 0, 0), ('h', 2, 0), ('v', 0, 0),
                     ('v', 0, 1), ('v', 1, 0), ('v', 1, 1)]:
            self.assertEqual(game.claim(*edge), ())
        self.assertEqual(game.claim('h', 1, 0), ((0, 0), (1, 0)))
        self.assertEqual(list(game.boxes), [1, 1])
        self.assertEqual(game.scores, [2, 0])
        self.assertEqual(game.current_player, 1)

    def test_one_box_game_and_finished_moves_do_not_change_state(self):
        game = Game(1, 1)
        for edge in [('h', 0, 0), ('h', 1, 0), ('v', 0, 0)]:
            game.claim(*edge)
        self.assertIsNone(game.winner)
        self.assertEqual(game.claim('v', 0, 1), ((0, 0),))
        self.assertEqual(game.winner, 2)
        self.assertEqual(game.scores, [0, 1])
        self.assertTrue(game.finished)
        before = snapshot(game)
        for edge in edges_for(game):
            self.assertIsNone(game.claim(*edge))
        with self.assertRaises(ValueError):
            game.claim('h', -1, 0)
        self.assertEqual(snapshot(game), before)

    def test_tied_game(self):
        game = Game(1, 2)
        for edge in [('h', 0, 0), ('v', 0, 0), ('h', 1, 0),
                     ('h', 2, 0), ('v', 0, 1), ('v', 1, 0), ('v', 1, 1)]:
            game.claim(*edge)
        self.assertTrue(game.finished)
        self.assertEqual(game.scores, [1, 1])
        self.assertEqual(game.winner, 0)

    def test_reset_preserves_dimensions_and_clears_all_state(self):
        game = Game(3, 2)
        for edge in edges_for(game):
            game.claim(*edge)
        self.assertTrue(game.finished)
        game.reset()
        self.assertEqual(snapshot(game), snapshot(Game(3, 2)))
        self.assertEqual((game.cols, game.rows), (3, 2))

    def test_random_games_preserve_rules_after_every_move(self):
        rng = random.Random(2350)
        for cols, rows in [(1, 1), (2, 3), (5, 4), (8, 6)]:
            for round_number in range(10):
                with self.subTest(board=(cols, rows), round=round_number):
                    game = Game(cols, rows)
                    edges = edges_for(game)
                    rng.shuffle(edges)
                    for move, edge in enumerate(edges, 1):
                        player = game.current_player
                        old_boxes = bytes(game.boxes)
                        old_scores = list(game.scores)
                        completed = game.claim(*edge)
                        self.assertIsInstance(completed, tuple)
                        self.assertEqual(game.edge_owner(*edge), player)
                        self.assertEqual(game.moves, move)
                        self.assertEqual(game.scores[player - 1],
                                         old_scores[player - 1] + len(completed))
                        self.assertEqual(game.scores[2 - player], old_scores[2 - player])
                        self.assertEqual(game.current_player,
                                         player if completed else 3 - player)
                        self.assertLessEqual(len(completed), 2)
                        for row in range(rows):
                            for col in range(cols):
                                index = row * cols + col
                                surrounded = all(game.edge_owner(*side) for side in [
                                    ('h', row, col), ('h', row + 1, col),
                                    ('v', row, col), ('v', row, col + 1)])
                                self.assertEqual(bool(game.boxes[index]), surrounded)
                                if old_boxes[index]:
                                    self.assertEqual(game.boxes[index], old_boxes[index])
                                elif game.boxes[index]:
                                    self.assertEqual(game.boxes[index], player)
                                    self.assertIn((row, col), completed)
                        self.assertEqual(game.scores,
                                         [game.boxes.count(1), game.boxes.count(2)])
                        self.assertEqual(sum(bool(owner) for owner in
                                             game.horizontal + game.vertical), move)
                        self.assertEqual(game.finished, move == len(edges))
                        if not game.finished:
                            self.assertIsNone(game.winner)
                    self.assertEqual(sum(game.scores), cols * rows)
                    expected_winner = (0 if game.scores[0] == game.scores[1]
                                       else 1 if game.scores[0] > game.scores[1] else 2)
                    self.assertEqual(game.winner, expected_winner)


if __name__ == '__main__':
    unittest.main()
