"""Animated GIFs of the TIA's picture, in plain Python: what record.py writes
when the file's name ends in .gif.

Every pixel keeps Stella's exact color: the GIF's color table is the stream's
palette, and its pixels are the TIA color values themselves, so nothing is
dithered or blended. A TIA pixel is drawn 4 wide and 2 tall (the TV shape:
640 wide). Only the part of a frame that changed is stored, and a frame the
same as the one before it just stays on screen longer.

A GIF can't show 60 frames a second: its delays are in hundredths of a second,
and browsers slow anything under 2 hundredths down to 10. So a GIF runs at 50
frames a second, each showing the frame on the TV at that moment: from NTSC's
60, one frame in six is left out; PAL's 50 are all there. A GIF has no sound.

    w = Writer(f, height)          # f: a file open for writing bytes
    w.add(pixels, palette)         # once for every GIF frame (1/50 s)
    w.close()
"""
import struct

FPS = 50
DELAY = 100 // FPS   # hundredths of a second each GIF frame is shown
SCALE = (4, 2)       # GIF pixels a TIA pixel takes, across and down
WIDTH = 160          # TIA pixels in a row
MAX_DELAY = 0xFFFF   # the longest a GIF frame can be shown


def lzw(pixels, min_size=8):
    """GIF image data for pixels (bytes, each under 2 ** min_size): the LZW
    codes, packed lowest bit first, from a clear code to the end code."""
    clear = 1 << min_size
    first = clear + 2           # the first code the table gives a string
    out = bytearray()
    acc, bits = clear, min_size + 1  # bits not written yet, the oldest lowest
    size = min_size + 1
    free = first
    table = {}
    get = table.get
    it = iter(pixels)
    prefix = next(it)
    for c in it:
        key = prefix << 8 | c
        code = get(key)
        if code is not None:
            prefix = code
            continue
        acc |= prefix << bits
        bits += size
        if bits >= 32:
            out += (acc & 0xFFFFFFFF).to_bytes(4, "little")
            acc >>= 32
            bits -= 32
        table[key] = free
        if free == 1 << size:
            size += 1
        free += 1
        if free == 4096:        # the table is full: start a new one
            acc |= clear << bits
            bits += size
            table.clear()
            size, free = min_size + 1, first
        prefix = c
    acc |= prefix << bits
    bits += size
    if free == 1 << size and size < 12:
        size += 1               # the reader's table grows on that last code too
    acc |= (clear + 1) << bits
    bits += size
    out += acc.to_bytes((bits + 7) // 8, "little")
    return bytes(out)


def _sub_blocks(data):
    """data in the pieces of up to 255 bytes a GIF keeps it in, then the end."""
    return b"".join(bytes([len(data[i:i + 255])]) + data[i:i + 255]
                    for i in range(0, len(data), 255)) + b"\x00"


def changed(before, after, height):
    """The box (left, top, right, bottom; right and bottom not in it) of the
    TIA pixels that differ between two frames; None if none do."""
    rows = [y for y in range(height)
            if before[y * WIDTH:(y + 1) * WIDTH] != after[y * WIDTH:(y + 1) * WIDTH]]
    if not rows:
        return None
    diff = 0  # each row as a big number, byte 0 the highest: the ones that differ
    for y in rows:
        diff |= (int.from_bytes(before[y * WIDTH:(y + 1) * WIDTH], "big")
                 ^ int.from_bytes(after[y * WIDTH:(y + 1) * WIDTH], "big"))
    left = WIDTH - 1 - (diff.bit_length() - 1) // 8
    right = WIDTH - ((diff & -diff).bit_length() - 1) // 8
    return left, rows[0], right, rows[-1] + 1


def scaled(pixels, box):
    """The TIA pixels in box, each drawn SCALE[0] wide and SCALE[1] tall."""
    left, top, right, bottom = box
    sx, sy = SCALE
    rows = []
    for y in range(top, bottom):
        row = pixels[y * WIDTH + left:y * WIDTH + right]
        wide = bytearray(len(row) * sx)
        for i in range(sx):
            wide[i::sx] = row
        rows += [wide] * sy
    return b"".join(rows)


class Writer:
    """Writes an animated GIF of TIA frames, all height rows tall, to f (a
    file open for writing bytes). It loops for ever."""

    def __init__(self, f, height):
        self._f = f
        self.height = height
        self.frames = 0      # GIF frames added (FPS a second)
        self.bytes_out = 0
        self._pending = None  # [pixels, palette, delay]: written when the next one differs
        self._shown = None    # (pixels, palette) of the frame written last
        self._palette = None  # the GIF's own color table: the first frame's palette

    def add(self, pixels, palette):
        """Show a frame for one GIF frame time (1/FPS s). pixels: 160 x height
        TIA color values; palette: 256 x (R, G, B), the stream's PALT."""
        self.frames += 1
        p = self._pending
        if p and p[2] + DELAY <= MAX_DELAY and p[0] == pixels and p[1] == palette:
            p[2] += DELAY
            return
        self._flush()
        self._pending = [pixels, palette, DELAY]

    def close(self):
        """Write the last frame and the GIF's end. The file stays open."""
        self._flush()
        if self._shown is not None:
            self._write(b";")

    def _write(self, data):
        self._f.write(data)
        self.bytes_out += len(data)

    def _flush(self):
        if self._pending is None:
            return
        pixels, palette, delay = self._pending
        self._pending = None
        whole = (0, 0, WIDTH, self.height)
        if self._shown is None:
            self._palette = palette
            self._write(b"GIF89a" + struct.pack("<HHBBB", WIDTH * SCALE[0],
                                                 self.height * SCALE[1], 0xF7, 0, 0)
                        + palette
                        + b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00")  # loop for ever
            box = whole
        elif palette != self._shown[1]:
            box = whole  # every color may have changed
        else:
            box = changed(self._shown[0], pixels, self.height)
        own = palette != self._palette  # a palette of its own after the stream's changed
        left, top, right, bottom = box
        sx, sy = SCALE
        # Graphic control: the delay, and leave the frame there for the next to draw on
        self._write(b"\x21\xf9\x04\x04" + struct.pack("<H", delay) + b"\x00\x00"
                    + b"," + struct.pack("<HHHHB", left * sx, top * sy, (right - left) * sx,
                                         (bottom - top) * sy, 0x87 if own else 0)
                    + (palette if own else b"")
                    + b"\x08" + _sub_blocks(lzw(scaled(pixels, box))))
        self._shown = (pixels, palette)
