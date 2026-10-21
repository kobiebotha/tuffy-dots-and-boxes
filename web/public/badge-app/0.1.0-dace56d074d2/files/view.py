"""320 x 240 renderer using Badgeware's small raster drawing API."""

from logos import Logos


class View:
    STEP = 34
    TOP = 46

    def __init__(self, screen, color, font):
        self.screen = screen
        self.screen.font = font.sins
        self.bg = color.rgb(16, 24, 21)
        self.panel = color.rgb(25, 36, 30)
        self.guide = color.rgb(48, 67, 56)
        self.white = color.rgb(234, 244, 237)
        self.muted = color.rgb(153, 177, 161)
        self.yellow = color.rgb(245, 203, 104)
        self.error = color.rgb(255, 112, 108)
        self.players = (self.guide, color.rgb(62, 207, 142), color.rgb(109, 169, 255))
        self.fills = (self.bg, color.rgb(24, 59, 42), color.rgb(23, 45, 72))
        self.logos = Logos(screen, color)

    def rect(self, x, y, width, height, pen):
        self.screen.pen = pen
        self.screen.rectangle(x, y, width, height)

    def circle(self, x, y, radius, pen):
        self.screen.pen = pen
        self.screen.circle(x, y, radius)

    def text(self, value, x, y, pen, scale=1, center=False):
        self.screen.pen = pen
        if center:
            width, _ = self.screen.measure_text(value, scale)
            x -= int(width) // 2
        self.screen.text(value, x, y, scale)

    def edge(self, orientation, row, col, left, pen, thickness):
        x, y = left + col * self.STEP, self.TOP + row * self.STEP
        half = thickness // 2
        if orientation == "h":
            self.rect(x, y - half, self.STEP, thickness, pen)
        else:
            self.rect(x - half, y, thickness, self.STEP, pen)

    def brand(self, y, scale=1):
        words = (("DOTS", self.players[1]), (" & ", self.muted), ("BOXES", self.players[2]))
        widths = [int(self.screen.measure_text(word, scale)[0]) for word, _ in words]
        x = (320 - sum(widths)) // 2
        for (word, pen), width in zip(words, widths):
            self.text(word, x, y, pen, scale)
            x += width

    def draw(self, controller, local_seat=None):
        game = controller.game
        screen = self.screen
        screen.pen = self.bg
        screen.clear()
        left = (320 - game.cols * self.STEP) // 2

        # Scores keep their player labels as well as their colors.
        for player, x in ((1, 8), (2, 220)):
            self.rect(x, 3, 92, 31, self.panel)
            active = not game.finished and game.current_player == player
            if active:
                self.rect(x, 33, 92, 3, self.players[player])
            label = "P%d (ME)" % player if local_seat == player else "P%d" % player
            score = "%02d" % game.scores[player - 1]
            score_width, _ = screen.measure_text(score, 2)
            self.text(label, x + 8, 11, self.players[player])
            self.text(score, x + 84 - int(score_width), 6, self.white, 2)
        if game.finished:
            turn = "DRAW" if game.winner == 0 else "P%d WINS!" % game.winner
            turn_color = self.white if game.winner == 0 else self.players[game.winner]
        else:
            turn = "P%d'S TURN" % game.current_player
            turn_color = self.players[game.current_player]
        turn_scale = 2
        turn_width, turn_height = screen.measure_text(turn, turn_scale)
        if turn_width > 112 or turn_height > 31:
            turn_scale = 1
            turn_width, turn_height = screen.measure_text(turn, turn_scale)
        self.text(turn, 160 - int(turn_width) // 2,
                  3 + (31 - int(turn_height)) // 2, turn_color, turn_scale)

        for row in range(game.rows):
            for col in range(game.cols):
                owner = game.boxes[row * game.cols + col]
                if owner:
                    x, y = left + col * self.STEP, self.TOP + row * self.STEP
                    self.rect(x + 3, y + 3, self.STEP - 6, self.STEP - 6, self.fills[owner])
                    self.text(str(owner), x + self.STEP // 2, y + 11,
                              self.players[owner], center=True)
                    if controller.flash_ms and (row, col) in controller.flash_boxes:
                        inset = 5 if controller.flash_ms // 150 % 2 else 7
                        self.outline(x + inset, y + inset, self.STEP - 2 * inset,
                                     self.STEP - 2 * inset, self.players[owner])

        for row in range(game.rows + 1):
            for col in range(game.cols):
                owner = game.horizontal[row * game.cols + col]
                self.edge("h", row, col, left, self.players[owner], 4 if owner else 1)
        for row in range(game.rows):
            for col in range(game.cols + 1):
                owner = game.vertical[row * (game.cols + 1) + col]
                self.edge("v", row, col, left, self.players[owner], 4 if owner else 1)

        pending = controller.pending_edge
        if (pending is not None and controller.pending_player in (1, 2)
                and not game.edge_owner(*pending)):
            # The durable local move stays visible until the server resolves it.
            orientation, row, col = pending
            self.edge(orientation, row, col, left, self.players[controller.pending_player], 4)

        if controller.selecting and controller.preview is not None:
            orientation, row, col = controller.preview
            owner = game.edge_owner(orientation, row, col)
            self.edge(orientation, row, col, left, self.error if owner else self.yellow, 8)
            if owner:
                # Leave the original owner visible inside the error outline.
                self.edge(orientation, row, col, left, self.players[owner], 4)

        for row in range(game.rows + 1):
            for col in range(game.cols + 1):
                self.circle(left + col * self.STEP, self.TOP + row * self.STEP, 3, self.white)

        if not game.finished:
            x, y = left + controller.col * self.STEP, self.TOP + controller.row * self.STEP
            # A square cursor becomes a solid ring while the origin is selected.
            if controller.selecting:
                self.circle(x, y, 7, self.yellow)
                self.circle(x, y, 4, self.bg)
                self.circle(x, y, 2, self.white)
            else:
                self.outline(x - 7, y - 7, 15, 15, self.yellow)

        self.rect(8, 192, 304, 1, self.guide)
        if game.finished:
            message = "It's a tie!" if game.winner == 0 else "Player %d wins!" % game.winner
            help_text = "HOME: EXIT"
        elif not getattr(controller, "can_claim", True):
            message = "OTHER BADGE'S TURN"
            help_text = "A/C + UP/DOWN: LOOK"
        elif controller.selecting:
            if controller.preview is None:
                message = "CHOOSE AN EDGE"
            elif game.edge_owner(*controller.preview):
                message = "EDGE TAKEN - CHOOSE ANOTHER"
            else:
                message = "B: CLAIM THIS EDGE"
            help_text = "A/C + UP/DOWN: EDGE   HOLD B: CANCEL"
        else:
            message = "CHOOSE A DOT"
            help_text = "A/C + UP/DOWN: MOVE   B: SELECT"
        if controller.message_ms and not game.finished:
            message = controller.message
        status_line = getattr(controller, "status_line", "")
        if status_line:
            help_text = status_line
        self.text(message, 160, 198, self.white, center=True)
        self.text(help_text, 160, 210, self.muted, center=True)
        self.text("HOLD UP 2s: LEAVE ROOM", 160, 230, self.muted, center=True)
        self.logos.draw()

    def pairing(self, code, status):
        self.screen.pen = self.bg
        self.screen.clear()
        self.brand(8, 2)
        self.text("ENTER PAIRING CODE", 160, 36, self.muted, center=True)
        shown = (code + "_____")[:5]
        self.rect(26, 74, 268, 50, self.panel)
        self.rect(26, 123, 134, 2, self.players[1])
        self.rect(160, 123, 134, 2, self.players[2])
        self.text(" ".join(shown), 160, 88, self.players[1], 3, center=True)
        self.text(status or "A  B  C ADD A LETTER", 160, 150, self.white, center=True)
        self.text("UP DELETES    FIFTH LETTER SENDS", 160, 190, self.muted, center=True)
        self.text("THE TUFTY ARCADE", 160, 220, self.muted, center=True)
        self.logos.draw()

    def message(self, title, body):
        self.screen.pen = self.bg
        self.screen.clear()
        self.brand(12, 2)
        self.text(title, 160, 80, self.white, center=True)
        self.text(body, 160, 120, self.muted, center=True)
        self.text("HOME: EXIT", 160, 200, self.muted, center=True)
        self.logos.draw()

    def outline(self, x, y, width, height, pen):
        self.rect(x, y, width, 2, pen)
        self.rect(x, y + height - 2, width, 2, pen)
        self.rect(x, y, 2, height, pen)
        self.rect(x + width - 2, y, 2, height, pen)
