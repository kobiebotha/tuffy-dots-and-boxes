"""Small pixel versions of the official PowerSync and Supabase logomarks.

Masks and quantized gradient colors come from the repository's
web/public/brand/powersync.svg and web/public/brand/supabase.svg. The sprite
rows are opaque palette indices (ASCII zero is transparent), so drawing needs
only Badgeware rectangles and no image decoder or hardware-specific imports.
"""


_PALETTE = (
    (0, 85, 255),
    (80, 50, 255),
    (146, 17, 255),
    (0, 150, 255),
    (0, 213, 255),
    (91, 184, 136),
    (41, 150, 100),
)

_POWERSYNC = (
    b"00003333333320000000000000",
    b"00033333333222000000000000",
    b"00033000000222000000000000",
    b"00333000000022200444440000",
    b"00330011111002200044444000",
    b"03300011111002220000444000",
    b"33300111100000220000044500",
    b"33300111110000022000005500",
    b"33300100110000022100005550",
    b"03330000144000002100100555",
    b"00330000044000001111100555",
    b"00333000004400000111100555",
    b"00033200004440011111000550",
    b"00032222000440011111005500",
    b"00002222200444000000055500",
    b"00000000000044400000055000",
    b"00000000000044444555555000",
    b"00000000000004445555550000",
)

_SUPABASE = (
    b"0000600000",
    b"0006600000",
    b"0066600000",
    b"0066600000",
    b"0666677666",
    b"6666677660",
    b"0000077600",
    b"0000076600",
    b"0000076000",
    b"0000060000",
)


class Logos:
    """Cache pens once, then draw the marks in the screen's bottom corners."""

    def __init__(self, screen, color):
        self.screen = screen
        self.pens = (None,) + tuple(color.rgb(*rgb) for rgb in _PALETTE)

    def draw(self):
        # PowerSync: 26 x 18 at (6, 220); Supabase: 20 x 20 at (294, 218).
        # Both end at y=238 and leave the centered room-exit hint unobstructed.
        self._draw(_POWERSYNC, 6, 220, 1)
        self._draw(_SUPABASE, 294, 218, 2)

    def _draw(self, rows, x, y, size):
        screen = self.screen
        for row_index, row in enumerate(rows):
            col = 0
            while col < len(row):
                index = row[col]
                start = col
                col += 1
                while col < len(row) and row[col] == index:
                    col += 1
                if index != 48:
                    screen.pen = self.pens[index - 48]
                    screen.rectangle(x + start * size, y + row_index * size,
                                     (col - start) * size, size)
