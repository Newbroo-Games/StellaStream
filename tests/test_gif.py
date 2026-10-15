"""Tests of animated GIFs (gif.py, record.record_gif()), on streams built
here, read back by a GIF reader of the tests' own and, when it is there, by
ffmpeg. GIFs need no ffmpeg and no Stella.

    python -m unittest discover -s tests -t .
"""
import io
import random
import shutil
import struct
import subprocess
import sys
import unittest
from pathlib import Path

import gif
import record
from tests import testrom
from tests.test_sscap import BUILD, ROOT, game, pack, run
from tests.test_stream import audio, chunk, head, palette

PALETTE = palette()[8:]  # the 768 bytes test_stream's PALT chunk carries
BASE, BAR = 0x1E, 0x44   # test_sscap's frames: this color with a bar of that one


def rgb(color, pal=PALETTE):
    return pal[color * 3:color * 3 + 3]


def fram(number, pixels, height):
    return chunk("FRAM", struct.pack("<QIHHHH", 0, number, 262, 23, 160, height) + pixels)


def pictures(*frames, height=4, pal=None):
    """A stream of NTSC frames with these pixels, each after a frame of sound."""
    data = head() + (chunk("PALT", pal) if pal else palette())
    for n, px in enumerate(frames):
        if isinstance(px, bytes) and len(px) == 768:  # a new palette
            data += chunk("PALT", px)
            continue
        data += audio(524) + fram(n, px, height)
    return data + chunk("END ")


def unlzw(data, min_size):
    """The pixels in GIF image data, read the usual way."""
    clear, end = 1 << min_size, (1 << min_size) + 1
    size, table, prev, out = min_size + 1, None, None, bytearray()
    acc = bits = 0
    it = iter(data)
    while True:
        while bits < size:
            acc |= next(it) << bits
            bits += 8
        code, acc, bits = acc & ((1 << size) - 1), acc >> size, bits - size
        if code == clear:
            table = [bytes([i]) for i in range(clear)] + [b"", b""]
            size, prev = min_size + 1, None
            continue
        if code == end:
            return bytes(out)
        if prev is None:
            entry = table[code]
        else:
            entry = table[code] if code < len(table) else prev + prev[:1]
            if len(table) < 4096:
                table.append(prev + entry[:1])
        out += entry
        if len(table) == 1 << size and size < 12:
            size += 1
        prev = entry


def read_gif(data):
    """(width, height, global table, loops, [frames]): each frame a dict of
    delay, disposal, box (x, y, w, h), own (a color table of its own) and
    picture (the whole screen after it, RGB)."""
    assert data[:6] == b"GIF89a", data[:6]
    width, height, packed, _, _ = struct.unpack_from("<HHBBB", data, 6)
    pos = 13
    table = None
    if packed & 0x80:
        n = 3 << ((packed & 7) + 1)
        table, pos = data[pos:pos + n], pos + n
    screen = bytearray(width * height * 3)
    frames, control, loops = [], {}, None

    def sub_blocks(pos):
        out = bytearray()
        while data[pos]:
            out += data[pos + 1:pos + 1 + data[pos]]
            pos += 1 + data[pos]
        return bytes(out), pos + 1

    while True:
        kind = data[pos]
        if kind == 0x3B:
            assert pos == len(data) - 1, "nothing after the end"
            return width, height, table, loops, frames
        if kind == 0x21:
            label = data[pos + 1]
            body, pos = sub_blocks(pos + 2)
            if label == 0xF9:
                control = {"disposal": (body[0] >> 2) & 7,
                           "delay": struct.unpack_from("<H", body, 1)[0]}
            elif label == 0xFF and body.startswith(b"NETSCAPE2.0"):
                loops = struct.unpack_from("<H", body, 12)[0]
            continue
        assert kind == 0x2C, hex(kind)
        x, y, w, h, packed = struct.unpack_from("<HHHHB", data, pos + 1)
        pos += 10
        colors = table
        if packed & 0x80:
            n = 3 << ((packed & 7) + 1)
            colors, pos = data[pos:pos + n], pos + n
        min_size = data[pos]
        image, pos = sub_blocks(pos + 1)
        pixels = unlzw(image, min_size)
        assert len(pixels) == w * h, (len(pixels), w, h)
        for row in range(h):
            line = b"".join(colors[c * 3:c * 3 + 3] for c in pixels[row * w:(row + 1) * w])
            start = ((y + row) * width + x) * 3
            screen[start:start + w * 3] = line
        frames.append(dict(control, box=(x, y, w, h), own=bool(packed & 0x80),
                           picture=bytes(screen)))
        control = {}


def make(stream, **options):
    """stream (bytes) as a GIF: (Result, read_gif()'s answer)."""
    BUILD.mkdir(parents=True, exist_ok=True)
    path = BUILD / "gif_test.gif"
    result = record.record_gif(io.BytesIO(stream), path, log=lambda m: None, **options)
    return result, read_gif(path.read_bytes())


def bar_at(picture, width=640):
    """Where the bar is on the top row of a picture: the TIA pixel."""
    row = picture[:width * 3]
    return next(x for x in range(width) if row[x * 3:x * 3 + 3] == rgb(BAR)) // 4


class Lzw(unittest.TestCase):
    def test_round_trip(self):
        rng = random.Random(2600)
        for n in list(range(1, 700)) + [4093, 4094, 4095, 4096, 4097, 20000, 70000]:
            for colors in (2, 9, 256):
                pixels = bytes(rng.randrange(colors) for _ in range(n))
                self.assertEqual(unlzw(gif.lzw(pixels), 8), pixels, (n, colors))
        for pixels in (bytes(100000), bytes(range(256)) * 300):
            self.assertEqual(unlzw(gif.lzw(pixels), 8), pixels)


class Gif(unittest.TestCase):
    def test_tv_shape_and_stellas_own_colors(self):
        result, (width, height, table, loops, frames) = make(game(3, height=40))
        self.assertEqual((width, height, loops), (640, 80, 0))
        self.assertEqual(table, PALETTE, "the GIF's colors are the stream's palette")
        self.assertEqual(frames[0]["box"], (0, 0, 640, 80), "the first frame is whole")
        expect = bytearray()
        for row in range(80):
            for x in range(640):
                expect += rgb(BAR if x // 4 == 0 else BASE)
        self.assertEqual(frames[0]["picture"], bytes(expect))
        self.assertEqual(result.frames, 3)

    def test_60_frames_a_second_shown_at_50(self):
        result, (_, _, _, _, frames) = make(game(120))
        self.assertEqual((result.frames, result.seconds), (100, 2.0))
        self.assertEqual([f["delay"] for f in frames], [2] * 100)
        self.assertEqual([f["disposal"] for f in frames], [1] * 100, "each draws on the last")
        shown = [bar_at(f["picture"]) for f in frames]
        self.assertEqual(shown, [g * 6 // 5 for g in range(100)])
        self.assertNotIn(5, shown, "one frame in six is left out")
        self.assertEqual(frames[1]["box"], (0, 0, 8, 80), "only what changed: the bar's two columns")

    def test_pal_keeps_every_frame(self):
        result, (_, _, _, _, frames) = make(game(50, timing=1, lines=312))
        self.assertEqual(result.frames, 50)
        self.assertEqual([bar_at(f["picture"]) for f in frames], list(range(50)))

    def test_a_still_picture_is_one_frame_shown_longer(self):
        still = bytes([BASE]) * 640
        moved = bytes([BAR]) + still[1:]
        result, (_, _, _, _, frames) = make(pictures(*[still] * 60, moved, moved))
        self.assertEqual(result.frames, 52)
        self.assertEqual([f["delay"] for f in frames], [100, 4])
        self.assertEqual(frames[1]["box"], (0, 0, 4, 2))

    def test_only_the_change_is_stored(self):
        before = bytearray([BASE]) * (160 * 20)
        after = bytearray(before)
        for y in range(10, 13):
            after[y * 160 + 50:y * 160 + 60] = bytes([BAR]) * 10
        _, (_, _, _, _, frames) = make(pictures(bytes(before), bytes(after), height=20))
        self.assertEqual(frames[1]["box"], (200, 20, 40, 6))
        self.assertEqual(frames[1]["picture"][(21 * 640 + 200) * 3:][:3], rgb(BAR))
        self.assertEqual(gif.changed(before, before, 20), None)

    def test_a_new_palette(self):
        other = bytes(255 - b for b in PALETTE)
        px = bytes([BASE]) * 640
        _, (_, _, table, _, frames) = make(pictures(px, other, px, px[:-1] + bytes([BAR])))
        self.assertEqual(table, PALETTE)
        self.assertEqual([f["own"] for f in frames], [False, True, True])
        self.assertEqual(frames[1]["box"], (0, 0, 640, 8), "all of it: every color changed")
        self.assertEqual(frames[1]["picture"][:3], rgb(BASE, other))
        self.assertEqual(frames[2]["box"], (636, 6, 4, 2))

    def test_seconds_and_an_empty_stream(self):
        result, _ = make(game(120), seconds=1)
        self.assertEqual(result.frames, 50)
        path = BUILD / "gif_none.gif"
        with self.assertRaisesRegex(record.RecordError, "No frames"):
            record.record_gif(io.BytesIO(head() + palette()), path, log=lambda m: None)
        self.assertFalse(path.exists(), "no empty GIF is left")

    def test_record_and_the_log(self):
        notes = []
        path = BUILD / "gif_record.gif"
        result = record.record(io.BytesIO(game(60)), path, scale=2, stereo=True,
                               ffmpeg="no_such_ffmpeg", log=notes.append)
        self.assertEqual((result.path, result.frames, result.end_seen), (path, 50, False))
        self.assertIn("Picture 160x40 -> GIF 640x80, 60 fps shown at 50, no sound", notes)


class Captures(unittest.TestCase):
    def setUp(self):
        BUILD.mkdir(parents=True, exist_ok=True)
        self.capture = BUILD / "gif.sscap"
        self.capture.write_bytes(pack(game(180)))

    def cli(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "record.py"), *map(str, args)],
                              capture_output=True, text=True, cwd=ROOT)

    def test_a_clip_needs_no_ffmpeg(self):
        path = BUILD / "gif_clip.gif"
        result = record.record_capture(self.capture, path, ffmpeg="no_such_ffmpeg",
                                       start=1.0, end=2.0, log=lambda m: None)
        self.assertEqual(result.frames, 50)
        _, _, _, _, frames = read_gif(path.read_bytes())
        self.assertEqual([bar_at(f["picture"]) for f in frames][:6], [60, 61, 62, 63, 64, 66])

    def test_command_line(self):
        done = self.cli(self.capture, BUILD / "gif_cli.gif", "--from", "1", "--to", "2",
                        "--ffmpeg", "no_such_ffmpeg")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("(50 frames, 1.0 s)", done.stderr)
        rom = testrom.write(BUILD / "gif_test.bin")
        for args, message in (((BUILD / "never.gif", "--rom", rom), "a GIF is made from a capture"),
                              ((self.capture, BUILD / "never.gif", "--scale", "2"),
                               "always the TV shape at --scale 1")):
            done = self.cli(*args)
            self.assertEqual(done.returncode, 2, done.stderr)
            self.assertIn(message, done.stderr)
        with self.assertRaisesRegex(record.RecordError, "A GIF is made from a capture"):
            record.record_rom(rom, BUILD / "never.gif")
        self.assertFalse((BUILD / "never.gif").exists())


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class FfmpegReadsIt(unittest.TestCase):
    """Another reader than the tests' own: ffmpeg sees the same pictures."""

    def test_same_pictures(self):
        before = bytearray([BASE]) * (160 * 20)
        frames = []
        for n in range(12):
            px = bytearray(before)
            px[(n % 20) * 160 + n * 7:(n % 20) * 160 + n * 7 + 5] = bytes([BAR]) * 5
            frames.append(bytes(px))
        _, (width, height, _, _, ours) = make(pictures(*frames, height=20))
        done = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                               "-i", str(BUILD / "gif_test.gif"), "-fps_mode", "passthrough",
                               "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        size = width * height * 3
        theirs = [done.stdout[i:i + size] for i in range(0, len(done.stdout), size)]
        self.assertEqual(len(theirs), len(ours))
        self.assertEqual(theirs, [f["picture"] for f in ours])


if __name__ == "__main__":
    unittest.main()
