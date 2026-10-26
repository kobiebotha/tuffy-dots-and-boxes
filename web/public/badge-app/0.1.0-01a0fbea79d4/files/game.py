"""Small, hardware-independent dots-and-boxes rules for MicroPython.

Rows and columns name dots for edges and the top-left corner for boxes.
Horizontal edges run right from their dot; vertical edges run down.
Owners are 0 (unclaimed), 1 (player one), and 2 (player two).
"""


class Game:
    def __init__(self, cols=5, rows=4):
        if not isinstance(cols, int) or not isinstance(rows, int):
            raise ValueError("Board dimensions must be positive integers")
        if cols < 1 or rows < 1:
            raise ValueError("Board dimensions must be positive integers")
        self.cols = cols
        self.rows = rows
        self.reset()

    def reset(self):
        """Clear the board and give player one the first turn."""
        self.horizontal = bytearray(self.cols * (self.rows + 1))
        self.vertical = bytearray((self.cols + 1) * self.rows)
        self.boxes = bytearray(self.cols * self.rows)
        self.current_player = 1
        self.scores = [0, 0]
        self.moves = 0

    @property
    def finished(self):
        return self.moves == len(self.horizontal) + len(self.vertical)

    @property
    def winner(self):
        """Return None during play, 0 for a tie, or the winning player."""
        if not self.finished:
            return None
        if self.scores[0] == self.scores[1]:
            return 0
        return 1 if self.scores[0] > self.scores[1] else 2

    def _edge(self, orientation, row, col):
        if not isinstance(row, int) or not isinstance(col, int):
            raise ValueError("Edge coordinates must be integers")
        if orientation == "h":
            if 0 <= row <= self.rows and 0 <= col < self.cols:
                return self.horizontal, row * self.cols + col
        elif orientation == "v":
            if 0 <= row < self.rows and 0 <= col <= self.cols:
                return self.vertical, row * (self.cols + 1) + col
        else:
            raise ValueError("Edge orientation must be 'h' or 'v'")
        raise ValueError("Edge is outside the board")

    def edge_owner(self, orientation, row, col):
        edges, index = self._edge(orientation, row, col)
        return edges[index]

    def _complete_box(self, row, col):
        index = row * self.cols + col
        if self.boxes[index]:
            return False
        horizontal = row * self.cols + col
        vertical = row * (self.cols + 1) + col
        if (self.horizontal[horizontal]
                and self.horizontal[horizontal + self.cols]
                and self.vertical[vertical]
                and self.vertical[vertical + 1]):
            self.boxes[index] = self.current_player
            return True
        return False

    def claim(self, orientation, row, col):
        """Claim an edge and return the newly completed (row, col) boxes.

        A legal move returns a tuple, including () when it completes no box.
        An occupied edge or finished game returns None without changing state.
        Invalid coordinates or orientation raise ValueError.
        """
        edges, index = self._edge(orientation, row, col)
        if self.finished or edges[index]:
            return None

        edges[index] = self.current_player
        self.moves += 1
        completed = []
        if orientation == "h":
            if row > 0 and self._complete_box(row - 1, col):
                completed.append((row - 1, col))
            if row < self.rows and self._complete_box(row, col):
                completed.append((row, col))
        else:
            if col > 0 and self._complete_box(row, col - 1):
                completed.append((row, col - 1))
            if col < self.cols and self._complete_box(row, col):
                completed.append((row, col))

        if completed:
            self.scores[self.current_player - 1] += len(completed)
        else:
            self.current_player = 3 - self.current_player
        return tuple(completed)
