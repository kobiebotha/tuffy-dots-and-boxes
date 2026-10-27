"""Five-letter pairing codes. A, B, and C append. Up deletes. The fifth sends."""


class CodeEntry:
    def __init__(self):
        self.code = ""
        self.status = ""
        self.previous = set()
        self.ready = False

    def clear(self):
        self.code = ""
        self.status = ""

    def step(self, held):
        """Return a finished code once, or None while the player is still typing."""
        held = set(held)
        if not self.ready:
            self.ready = not held
            self.previous = held
            return None
        pressed = held - self.previous
        self.previous = held
        if len(pressed) != 1 or len(held) != 1:
            return None
        key = next(iter(pressed))
        if key == "UP":
            self.code = self.code[:-1]
            self.status = ""
            return None
        if key in ("A", "B", "C") and len(self.code) < 5:
            self.code += key
            self.status = ""
            if len(self.code) == 5:
                finished = self.code
                return finished
        return None
