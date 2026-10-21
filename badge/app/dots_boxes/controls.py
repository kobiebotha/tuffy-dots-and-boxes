"""Button-driven interaction, independent of the badge and its clock."""

from game import Game


DIRECTIONS = ("left", "right", "up", "down")
DELTAS = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}


class Controller:
    CANCEL_MS = 650
    REPEAT_DELAY_MS = 350
    REPEAT_MS = 110

    def __init__(self, game=None):
        self.game = game if game is not None else Game()
        self.col = self.game.cols // 2
        self.row = self.game.rows // 2
        self.selecting = False
        self.preview = None
        self.message = "P1 starts. Choose a dot."
        self.message_ms = 1800
        self.flash_boxes = ()
        self.flash_ms = 0
        self.previous = set()
        # Ignore the button that launched the app until it has been released.
        self.wait_for_release = True
        self.b_ms = 0
        self.b_cancelled = False
        self.repeat_direction = None
        self.repeat_ms = 0
        self.can_claim = True
        self.on_claim = None
        self.status_line = ""
        self.pending_edge = None
        self.pending_player = 0

    def say(self, message, duration=1400):
        self.message = message
        self.message_ms = duration

    def edge_in_direction(self, direction):
        dx, dy = DELTAS[direction]
        col, row = self.col + dx, self.row + dy
        if not (0 <= col <= self.game.cols and 0 <= row <= self.game.rows):
            return None
        if dx:
            return ("h", self.row, min(col, self.col))
        return ("v", min(row, self.row), self.col)

    def direction(self, direction):
        if self.game.finished:
            return
        self.message_ms = 0
        if self.selecting:
            # Clear the old preview even when the new direction is off-board.
            self.preview = self.edge_in_direction(direction)
            if self.preview is None:
                self.say("Board boundary. Choose another.")
            elif self.game.edge_owner(*self.preview):
                self.say("Taken. Choose another edge.")
        else:
            dx, dy = DELTAS[direction]
            self.col = max(0, min(self.game.cols, self.col + dx))
            self.row = max(0, min(self.game.rows, self.row + dy))

    def tap(self):
        if self.game.finished:
            self.say("Game over. Make a new room on the web.")
            return
        if not self.can_claim:
            self.say("Waiting for the other badge.")
            return
        if not self.selecting:
            for direction in DIRECTIONS:
                edge = self.edge_in_direction(direction)
                if edge is not None and self.game.edge_owner(*edge) == 0:
                    self.selecting = True
                    self.preview = None
                    self.message_ms = 0
                    return
            self.say("No free edges here. Move a dot.")
            return
        if self.preview is None:
            self.say("Choose a direction first.")
            return
        if self.game.edge_owner(*self.preview):
            self.say("Taken. Choose another edge.")
            return
        if self.on_claim is not None:
            if self.on_claim(self.preview):
                self.selecting = False
                self.preview = None
                self.say("Sending move...")
            return
        player = self.game.current_player
        completed = self.game.claim(*self.preview)
        if completed is None:
            self.say("Taken. Choose another edge.")
            return
        self.selecting = False
        self.preview = None
        self.flash_boxes = completed
        self.flash_ms = 900 if completed else 0
        if self.game.finished:
            self.say("Board complete!", 1800)
        elif completed:
            self.say("P%d +%d! Play again." % (player, len(completed)), 2000)
        else:
            self.say("P%d's turn. Pass the badge." % self.game.current_player)

    def cancel(self):
        if self.selecting:
            self.selecting = False
            self.preview = None
            self.say("Selection cancelled.", 900)

    def step(self, held, elapsed_ms):
        """Advance once per frame. `held` contains left/right/up/down/b."""
        elapsed_ms = max(0, elapsed_ms)
        self.message_ms = max(0, self.message_ms - elapsed_ms)
        self.flash_ms = max(0, self.flash_ms - elapsed_ms)
        held = set(held)
        if self.wait_for_release:
            self.wait_for_release = bool(held)
            self.previous = held
            return

        b_down = "b" in held
        b_was_down = "b" in self.previous
        if b_down:
            if not b_was_down:
                self.b_ms = 0
                self.b_cancelled = False
            else:
                self.b_ms += elapsed_ms
            if self.b_ms >= self.CANCEL_MS and not self.b_cancelled:
                self.cancel()
                self.b_cancelled = True
        elif b_was_down:
            # Commit on release: a hold must never claim before it cancels.
            self.b_ms += elapsed_ms
            if self.b_ms >= self.CANCEL_MS:
                if not self.b_cancelled:
                    self.cancel()
            elif not self.b_cancelled:
                self.tap()

        directions = [key for key in DIRECTIONS if key in held]
        if b_down or b_was_down or len(directions) != 1:
            self.repeat_direction = None
        else:
            direction = directions[0]
            if direction != self.repeat_direction:
                self.direction(direction)
                self.repeat_direction = direction
                self.repeat_ms = self.REPEAT_DELAY_MS
            elif not self.selecting:
                self.repeat_ms -= elapsed_ms
                if self.repeat_ms <= 0:
                    self.direction(direction)
                    self.repeat_ms = self.REPEAT_MS
        self.previous = held
