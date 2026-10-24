"""Byte transcripts of inquire 0.9.4's incremental frame redraw (issue #27).

FrameRenderer keeps the rows of the last frame and the cursor position. The
next frame goes back to the top of the old one with RELATIVE moves, rewrites
only the rows that changed (each followed by "erase to end of line"), erases
rows that vanished and parks the cursor at the input position of the prompt
row. A row that did not change is not sent again: a reader of the byte stream
that only looks at text lines sees a mix of old and new rows.
"""


class InquireFrames:
    def __init__(self):
        self.rows = []
        self.cursor_row = 0
        self.cursor_col = 0

    def draw(self, rows, cursor_row, cursor_col):
        """Bytes (as text) that turn the current frame into `rows`"""
        out = "\x1b[?25l"
        if self.cursor_row:
            out += "\x1b[%dA" % self.cursor_row
        if self.cursor_col:
            out += "\x1b[%dD" % self.cursor_col
        height = max(len(rows), len(self.rows))
        for index in range(height):
            if index >= len(rows):
                out += "\x1b[2K"
            elif index >= len(self.rows) or self.rows[index] != rows[index]:
                out += rows[index] + "\x1b[K"
            out += "\r"
            if index < height - 1:
                out += "\n"
        if height - 1 - cursor_row:
            out += "\x1b[%dA" % (height - 1 - cursor_row)
        if cursor_col:
            out += "\x1b[%dC" % cursor_col
        out += "\x1b[?25h"
        self.rows = list(rows)
        self.cursor_row = cursor_row
        self.cursor_col = cursor_col
        return out

    def finish(self, row_text):
        """The answered prompt: the frame shrinks to one row, then a newline"""
        out = self.draw([row_text], 0, len(row_text))
        self.rows = []
        self.cursor_row = self.cursor_col = 0
        return out + "\r\n"
