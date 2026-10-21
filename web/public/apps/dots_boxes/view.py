"""320 x 240 renderer using Badgeware's small raster drawing API."""


class View:
    STEP = 34
    TOP = 46

    def __init__(self, screen, color, font):
        self.screen = screen
        self.screen.font = font.sins
        self.bg = color.rgb(14, 20, 30)
        self.panel = color.rgb(23, 32, 46)
        self.guide = color.rgb(43, 55, 71)
        self.white = color.rgb(238, 244, 250)
        self.muted = color.rgb(159, 177, 196)
        self.yellow = color.rgb(255, 220, 93)
        self.error = color.rgb(255, 112, 108)
        self.players = (self.guide, color.rgb(61, 220, 204), color.rgb(249, 134, 180))
        self.fills = (self.bg, color.rgb(21, 68, 70), color.rgb(72, 37, 59))

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

    def draw(self, controller):
        game = controller.game
        screen = self.screen
        screen.pen = self.bg
        screen.clear()
        left = (320 - game.cols * self.STEP) // 2

        # Scores keep their player labels as well as their colors.
        for player, x in ((1, 8), (2, 230)):
            self.rect(x, 3, 82, 31, self.panel)
            active = not game.finished and game.current_player == player
            if active:
                self.rect(x, 33, 82, 3, self.players[player])
            self.text("P%d" % player, x + 8, 11, self.players[player])
            self.text("%02d" % game.scores[player - 1], x + 38, 6, self.white, 2)
        self.text("DOTS & BOXES", 160, 4, self.muted, center=True)
        if game.finished:
            turn = "DRAW" if game.winner == 0 else "P%d WINS!" % game.winner
            turn_color = self.white if game.winner == 0 else self.players[game.winner]
        else:
            turn = "P%d'S TURN" % game.current_player
            turn_color = self.players[game.current_player]
        self.text(turn, 160, 22, turn_color, center=True)

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
        self.text(help_text, 160, 220, self.muted, center=True)

    def pairing(self, code, status):
        self.screen.pen = self.bg
        self.screen.clear()
        self.text("ENTER PAIRING CODE", 160, 36, self.muted, center=True)
        shown = (code + "_____")[:5]
        self.text(" ".join(shown), 160, 88, self.yellow, 3, center=True)
        self.text(status or "A  B  C ADD A LETTER", 160, 150, self.white, center=True)
        self.text("UP DELETES    FIFTH LETTER SENDS", 160, 190, self.muted, center=True)

    def message(self, title, body):
        self.screen.pen = self.bg
        self.screen.clear()
        self.text(title, 160, 80, self.white, center=True)
        self.text(body, 160, 120, self.muted, center=True)
        self.text("HOME: EXIT", 160, 200, self.muted, center=True)

    def outline(self, x, y, width, height, pen):
        self.rect(x, y, width, 2, pen)
        self.rect(x, y + height - 2, width, 2, pen)
        self.rect(x, y, 2, height, pen)
        self.rect(x + width - 2, y, 2, height, pen)
