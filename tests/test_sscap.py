"""Tests of capture files (.sscap): the writer, the reader and the spec's own
decoder, on streams built here. One test records the test ROM with Stella
(silently, on its own settings folder); it is skipped without one.

    python -m unittest discover -s tests -t .
"""
import inspect
import io
import random
import re
import shutil
import struct
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

import rawstream
import record
import sscap
from rawstream import Header, Palette, close_stella, launch_stella, parse
from sscap import CUT, HAS_END, HAS_HEAD, HAS_JUMP, HAS_PALT, Capture, CaptureError, Writer
from tests import testrom
from tests.test_stream import audio, chunk, head, palette, run_of_frames

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "tests"
SPEC = ROOT / "docs" / "capture-format.md"
METHODS = [sscap.STORED, sscap.ZLIB, sscap.XZ] + ([sscap.ZSTD] if sscap.zstd else [])
LINE_CYCLES = 76
NTSC = 262 * LINE_CYCLES  # cycles in one NTSC frame


def bar_frame(number, height=40, lines=262, cycles=0):
    """A frame with a one-pixel bar that moves a pixel a frame."""
    px = bytearray([0x1E]) * (160 * height)
    for row in range(height):
        px[row * 160 + number % 160] = 0x44
    return chunk("FRAM", struct.pack("<QIHHHH", cycles, number, lines, 23, 160, height)
                 + bytes(px))


def run(count, start=0, lines=262, height=40, cycles=0):
    """count frames, each after its sound and RAM, as Stella sends them."""
    data = b""
    for n in range(start, start + count):
        cycles += lines * LINE_CYCLES
        data += (audio(2 * lines, value=0x21 + n % 3) + chunk("RAM ", bytes([n & 255]) + bytes(127))
                 + bar_frame(n, height, lines, cycles))
    return data


def game(count, timing=0, lines=262, height=40):
    return head(timing, 31440 if timing == 0 else 31200) + palette() + run(count, 0, lines, height)


# 170 frames: 70, an unknown chunk, a state load (Stella's own frame number and
# cycle count go back), 50, 30 at another height, a ROM load with its new HEAD
# and PALT, 20, END. Blocks of one second: 60, 60, 50.
MIXED = (game(70) + chunk("ZZZZ", b"a later chunk") + chunk("JUMP", b"\x01")
         + run(50, start=10) + run(30, start=60, height=30) + chunk("JUMP", b"\x02")
         + head() + palette() + run(20) + chunk("END "))


def pack(raw, piece=None, **options):
    """raw packed into a capture, written in pieces of `piece` bytes."""
    out = io.BytesIO()
    w = Writer(out, **options)
    step = piece or max(len(raw), 1)
    for i in range(0, len(raw), step):
        w.write(raw[i:i + step])
    w.close()
    return out.getvalue()


def opened(data):
    return Capture(io.BytesIO(data))


def unpack(data):
    return opened(data).stream().read()


def damaged(data, offset):
    """data with the byte at offset changed."""
    b = bytearray(data)
    b[offset] ^= 0x5A
    return bytes(b)


class RoundTrip(unittest.TestCase):
    def test_every_method(self):
        for method in METHODS:
            with self.subTest(method=sscap.METHOD_NAMES[method]):
                self.assertEqual(unpack(pack(MIXED, method=method)), MIXED)

    def test_delta_tags(self):
        for tags in ([], ["FRAM"], ["FRAM", "RAM", "AUDI"]):
            with self.subTest(tags=tags):
                data = pack(MIXED, delta_tags=tags)
                self.assertEqual(unpack(data), MIXED)
                self.assertEqual(opened(data).delta_tags, tuple(sscap.tag_bytes(t) for t in tags))
        self.assertEqual(opened(pack(MIXED)).delta_tags, (b"FRAM", b"RAM "),
                         "picture and RAM by default (the smallest files in our tests)")

    def test_the_xor_reaches_the_frame_before(self):
        # A busy 33.6 KB picture where only a bar moves: zlib's 32 KB window
        # can't reach the frame before by itself, the XOR brings it in
        noise = random.Random(1).randbytes(160 * 210)
        raw = head() + palette()
        for n in range(20):
            px = bytearray(noise)
            for row in range(210):
                px[row * 160 + n] = 0x44
            raw += chunk("FRAM", struct.pack("<QIHHHH", 0, n, 262, 23, 160, 210) + px)
        with_xor = len(pack(raw, method=sscap.ZLIB))
        without = len(pack(raw, method=sscap.ZLIB, delta_tags=[]))
        self.assertLess(with_xor * 10, without)

    def test_pieces_of_any_size(self):
        raw = game(5, height=2) + chunk("END ")
        whole = pack(raw)
        for piece in (1, 7, 1000):
            with self.subTest(piece=piece):
                self.assertEqual(pack(raw, piece=piece), whole)

    def test_cut_off_stream(self):
        stream = game(65)
        next_chunk = audio(524)
        for cut, where in ((len(stream) - 100, "inside a frame"),
                           (len(stream) + 3, "inside a chunk's tag")):
            with self.subTest(where):
                raw = (stream + next_chunk)[:cut]
                cap = opened(pack(raw))
                self.assertEqual(cap.stream().read(), raw)
                self.assertTrue(cap.blocks[-1].flags & CUT)
                self.assertFalse(any(b.flags & CUT for b in cap.blocks[:-1]))

    def test_background_writer_writes_the_same_file(self):
        self.assertEqual(pack(MIXED, piece=5000, background=True), pack(MIXED))

    def test_background_writer_reports_a_full_disk(self):
        class FullDisk(io.BytesIO):
            def write(self, data):
                if self.tell() + len(data) > 20000:
                    raise OSError(28, "No space left on device")
                return super().write(data)

        w = Writer(FullDisk(), method=sscap.STORED, background=True)
        with self.assertRaisesRegex(OSError, "No space"):
            for i in range(0, len(MIXED), 5000):
                w.write(MIXED[i:i + 5000])
            w.close()

    def test_empty_stream(self):
        cap = opened(pack(b""))
        self.assertTrue(cap.indexed)
        self.assertEqual((cap.blocks, cap.frames, cap.stream().read()), ([], 0, b""))


class Blocks(unittest.TestCase):
    def test_one_second_ntsc_and_pal(self):
        ntsc = opened(pack(game(130)))
        self.assertEqual([b.frames for b in ntsc.blocks], [60, 60, 10])
        self.assertEqual([b.capture_time for b in ntsc.blocks], [0, 60 * NTSC, 120 * NTSC])
        pal = opened(pack(game(110, timing=1, lines=312)))
        self.assertEqual([b.frames for b in pal.blocks], [50, 50, 10])

    def test_block_seconds(self):
        cap = opened(pack(game(70), block_seconds=0.5))
        self.assertEqual([b.frames for b in cap.blocks], [30, 30, 10])

    def test_flags_and_totals(self):
        cap = opened(pack(MIXED))
        self.assertEqual([b.flags for b in cap.blocks],
                         [HAS_HEAD | HAS_PALT, HAS_JUMP, HAS_JUMP | HAS_HEAD | HAS_PALT | HAS_END])
        self.assertEqual([b.frame_index for b in cap.blocks], [0, 60, 120])
        self.assertEqual((cap.frames, cap.cycles), (170, 170 * NTSC))

    def test_capture_time_runs_on_across_a_jump(self):
        # After the state load Stella's own cycle count starts again from 0,
        # but the capture time keeps counting from the file's start
        cap = opened(pack(MIXED))
        self.assertEqual([b.capture_time for b in cap.blocks], [0, 60 * NTSC, 120 * NTSC])

    def test_recovery_copies(self):
        self.assertEqual(opened(pack(MIXED)).recovery,
                         [(0, head()), (0, palette()), (2, head()), (2, palette())])
        self.assertEqual(opened(pack(MIXED, recovery=False)).recovery, [])


class Reading(unittest.TestCase):
    def test_block_at(self):
        cap = opened(pack(MIXED))
        self.assertEqual([cap.block_at(frame=f) for f in (0, 59, 60, 119, 120, 169)],
                         [0, 0, 1, 1, 2, 2])
        self.assertEqual([cap.block_at(cycles=c) for c in (0, 60 * NTSC - 1, 60 * NTSC)],
                         [0, 0, 1])
        with self.assertRaises(IndexError):
            cap.block_at(frame=170)

    def test_stream_from_a_later_block(self):
        cap = opened(pack(game(130)))
        self.assertEqual(cap.stream(1).read(),
                         head() + palette() + cap.read_block(1) + cap.read_block(2))
        items = list(parse(cap.stream(2)))
        self.assertEqual([type(i) for i in items[:2]], [Header, Palette])
        self.assertEqual(sum(isinstance(i, rawstream.Frame) for i in items), 10)

    def test_file_without_index(self):
        data = pack(game(130))
        full = opened(data)
        cut = data[:full.blocks[2].offset + 10]  # the writer died writing block 2
        cap = opened(cut)
        self.assertFalse(cap.indexed)
        self.assertIn("cut off", cap.index_problem)
        self.assertEqual((len(cap.blocks), cap.frames, cap.cycles), (2, 120, 120 * NTSC))
        self.assertEqual(cap.stream().read(), full.read_block(0) + full.read_block(1))

    def test_damaged_index_falls_back_to_the_blocks(self):
        data = pack(MIXED)
        good = opened(data)
        cap = opened(damaged(data, len(data) - 40))  # inside the recovery copies
        self.assertFalse(cap.indexed)
        self.assertIn("CRC", cap.index_problem)
        self.assertEqual(cap.blocks, good.blocks)
        self.assertEqual((cap.frames, cap.cycles), (good.frames, good.cycles))
        self.assertEqual(cap.stream().read(), MIXED)

    def test_damaged_block(self):
        data = pack(game(130), method=sscap.STORED)
        good = opened(data)
        cap = opened(damaged(data, good.blocks[1].offset + 36 + 5000))
        with self.assertRaisesRegex(CaptureError, "block 1 is damaged"):
            cap.read_block(1)
        self.assertEqual(cap.read_block(0), good.read_block(0))
        self.assertEqual(cap.read_block(2), good.read_block(2))
        compressed = pack(game(130))
        cap = opened(damaged(compressed, opened(compressed).blocks[1].offset + 36 + 50))
        with self.assertRaisesRegex(CaptureError, "block 1"):
            cap.read_block(1)

    def test_recovery_when_block_0_is_damaged(self):
        for recovery in (True, False):
            with self.subTest(recovery=recovery):
                data = pack(game(130), recovery=recovery)
                cap = opened(damaged(data, opened(data).blocks[0].offset + 36 + 100))
                if recovery:
                    self.assertEqual(cap.context(1), (head(), palette()))
                    self.assertEqual(cap.stream(1).read()[:len(head() + palette())],
                                     head() + palette())
                else:
                    with self.assertRaisesRegex(CaptureError, "block 0"):
                        cap.context(1)

    def test_methods_it_cannot_read(self):
        data = pack(game(70), method=sscap.STORED)
        at = opened(data).blocks[0].offset + 16  # the block header's method byte
        for method, message in ((200, "private compression method 200"),
                                (5, "unknown compression method 5")):
            with self.subTest(method=method):
                b = bytearray(data)
                b[at] = method
                with self.assertRaisesRegex(CaptureError, message):
                    opened(bytes(b)).read_block(0)

    def test_not_a_capture_or_a_newer_version(self):
        with self.assertRaisesRegex(CaptureError, "not a StellaStream capture"):
            opened(game(5))
        data = bytearray(pack(game(5)))
        data[8] = 2  # format version 2
        with self.assertRaisesRegex(CaptureError, "version 2"):
            opened(bytes(data))


class Clips(unittest.TestCase):
    """Capture.clip(): the frames that start between two times, each with the
    chunks sent before it, after the HEAD and PALT in effect."""

    def test_times(self):
        for text, seconds in (("80", 80), ("80.5", 80.5), (".5", 0.5), ("1:20", 80),
                              ("01:20.25", 80.25), ("1:02:03.5", 3723.5), (" 0:00 ", 0)):
            with self.subTest(text):
                self.assertEqual(sscap.parse_time(text), seconds)
        for text in ("", "x", "-1", "1:60", "1:75:00", "1:2:3:4", "1.5:00", "nan", "inf",
                     "1e3", "1:"):
            with self.subTest(text):
                with self.assertRaises(ValueError):
                    sscap.parse_time(text)
        self.assertEqual([sscap.time_text(s) for s in (0, 80.5, 3723.5, 59.999)],
                         ["0:00.00", "1:20.50", "1:02:03.50", "1:00.00"])
        for s in (0, 1.25, 80.5, 3723.5):
            self.assertEqual(sscap.parse_time(sscap.time_text(s)), s)

    def test_a_clip_of_everything_is_the_stream(self):
        for raw in (MIXED, game(130)[:-1000]):
            with self.subTest(len(raw)):
                cap = opened(pack(raw))
                self.assertEqual(cap.clip().read(), raw)
                self.assertEqual(cap.clip(0, cap.seconds()).read(), raw)

    def test_a_clip_is_its_frames(self):
        cap = opened(pack(game(300)))  # frame n starts at n/60 s; blocks of 60 frames
        self.assertEqual(cap.clip(1.5, 2.5).read(),
                         head() + palette() + run(60, start=90, cycles=90 * NTSC))
        self.assertEqual(cap.clip(4.5).read(),
                         head() + palette() + run(30, start=270, cycles=270 * NTSC))
        self.assertEqual(cap.clip(4.0, 99).read(),
                         head() + palette() + run(60, start=240, cycles=240 * NTSC))

    def test_the_head_and_palette_in_effect(self):
        other = chunk("PALT", bytes(255 - i % 256 for i in range(768)))
        cap = opened(pack(game(100) + other + run(100, start=100, cycles=100 * NTSC)))
        self.assertEqual(cap.clip(1.0, 1.5).read(),
                         head() + palette() + run(30, start=60, cycles=60 * NTSC))
        self.assertEqual(cap.clip(2.0, 2.5).read(),
                         head() + other + run(30, start=120, cycles=120 * NTSC))
        # From the frame the new palette comes with: it isn't sent twice
        self.assertEqual(cap.clip(100 / 60, 110 / 60).read(),
                         head() + other + run(10, start=100, cycles=100 * NTSC))

    def test_chunks_before_a_frame_go_with_it(self):
        cap = opened(pack(MIXED))
        self.assertEqual(cap.clip(70 / 60, 71 / 60).read(),
                         head() + palette() + chunk("ZZZZ", b"a later chunk")
                         + chunk("JUMP", b"\x01") + run(1, start=10))
        self.assertEqual(cap.clip(2.0, 2.5).read(),
                         head() + palette() + run(30, start=60, height=30))
        before = len(game(70) + chunk("ZZZZ", b"a later chunk") + chunk("JUMP", b"\x01")
                     + run(50, start=10))
        self.assertEqual(cap.clip(2.0).read(), head() + palette() + MIXED[before:],
                         "to the end: the ROM load and END too")

    def test_times_outside_the_capture(self):
        cap = opened(pack(game(300)))
        with self.assertRaisesRegex(ValueError, r"after the capture's end \(0:05.00\)"):
            cap.clip(5.0)
        with self.assertRaisesRegex(ValueError, "before it starts"):
            cap.clip(2.0, 1.0)
        with self.assertRaises(ValueError):
            cap.clip(-1)

    def test_a_damaged_block_is_left_out(self):
        data = pack(game(300))
        cap = opened(damaged(data, opened(data).blocks[2].offset + 36 + 20))
        notes = []
        self.assertEqual(cap.clip(1.5, 3.5, on_damage=lambda i, e: notes.append(i)).read(),
                         head() + palette() + run(30, start=90, cycles=90 * NTSC)
                         + run(30, start=180, cycles=180 * NTSC))
        self.assertEqual(notes, [2])
        with self.assertRaises(CaptureError):
            cap.clip(1.5, 3.5).read()


class Spec(unittest.TestCase):
    """docs/capture-format.md and sscap.py must say the same thing."""

    def spec_code(self):
        text = SPEC.read_text(encoding="utf-8")
        return re.search(r"```python\n(.*?)```", text, re.S).group(1)

    def test_spec_decoder_is_the_one_in_use(self):
        code = self.spec_code()
        self.assertEqual(code[code.index("def decode_block"):].rstrip(),
                         inspect.getsource(sscap.decode_block).rstrip())
        spec = {}
        exec(code, spec)
        for method in METHODS:
            data = pack(MIXED, method=method, delta_tags=["FRAM", "RAM", "AUDI"])
            cap = opened(data)
            for i, b in enumerate(cap.blocks):
                with self.subTest(method=method, block=i):
                    _, stored, *_ = sscap.BLOCK_HEADER.unpack_from(data, b.offset)
                    start = b.offset + sscap.BLOCK_HEADER.size
                    self.assertEqual(spec["decode_block"](data[start:start + stored], method,
                                                          cap.delta_tags),
                                     cap.read_block(i))

    def test_layout_sizes(self):
        self.assertEqual([s.size for s in (sscap.FILE_HEADER, sscap.BLOCK_HEADER,
                                           sscap.INDEX_HEADER, sscap.INDEX_ENTRY, sscap.TRAILER)],
                         [16, 36, 24, 24, 16])
        data = pack(MIXED, delta_tags=["FRAM", "RAM"])
        self.assertEqual(data[:8], b"\x89SSC\r\n\x1a\n")
        self.assertEqual(struct.unpack_from("<HHBBBB", data, 8), (1, 24, 36, 24, 2, 0))
        self.assertEqual(data[16:24], b"FRAMRAM ")
        self.assertEqual(data[24:28], b"SSCB")
        self.assertEqual(data[-4:], b"SSCE")


class CommandLine(unittest.TestCase):
    def sscap(self, *args):
        return subprocess.run([sys.executable, str(ROOT / "sscap.py"), *map(str, args)],
                              capture_output=True, text=True, cwd=ROOT)

    def test_pack_unpack_report(self):
        BUILD.mkdir(parents=True, exist_ok=True)
        raw, packed, back = BUILD / "cli.raw", BUILD / "cli.sscap", BUILD / "cli_back.raw"
        raw.write_bytes(MIXED)
        done = self.sscap(raw, packed)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("3 blocks, 170 frames", done.stderr)
        done = self.sscap(packed, back)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(back.read_bytes(), MIXED)
        done = self.sscap(packed, "-v")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("every block decodes", done.stdout)
        self.assertIn("2.83 s of emulated time (NTSC", done.stdout)
        packed.write_bytes(damaged(packed.read_bytes(), opened(packed.read_bytes()).blocks[1].offset + 60))
        done = self.sscap(packed)
        self.assertEqual(done.returncode, 1)
        self.assertIn("1 of 3 blocks damaged", done.stdout)

    def test_clips(self):
        BUILD.mkdir(parents=True, exist_ok=True)
        source, clip = BUILD / "cli_clip_source.sscap", BUILD / "cli_clip.sscap"
        clip_raw, raw = BUILD / "cli_clip.raw", BUILD / "cli_clip_source.raw"
        source.write_bytes(pack(MIXED))
        raw.write_bytes(MIXED)
        expected = opened(pack(MIXED)).clip(1, 2).read()
        done = self.sscap(source, clip, "--from", "1", "--to", "0:02")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("the clip from 0:01.00 to 0:02.00", done.stderr)
        self.assertIn("1 blocks, 60 frames", done.stderr)
        self.assertEqual(opened(clip.read_bytes()).stream().read(), expected)
        done = self.sscap(source, clip_raw, "--from", "1", "--to", "2")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(clip_raw.read_bytes(), expected)
        for args, code, message in (((source, clip, "--from", "1:99"), 2, "not a time"),
                                    ((source, clip, "--from", "9"), 1, "after the capture's end"),
                                    ((raw, clip, "--from", "1"), 2, "pack the stream first"),
                                    ((source, "--from", "1"), 2, "a file to write"),
                                    ((source, source, "--from", "1"), 2, "the one being read"),
                                    ((source, BUILD / "never.mp4"), 2, "For a video: python record.py"),
                                    ((source, BUILD / "never.gif"), 2, "For a GIF: python record.py")):
            with self.subTest(message):
                done = self.sscap(*args)
                self.assertEqual(done.returncode, code, done.stderr)
                self.assertIn(message, done.stderr)
        self.assertEqual(source.read_bytes(), pack(MIXED))


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class Video(unittest.TestCase):
    """record.py makes a video from a capture as from a raw stream."""

    def capture(self, name, raw, damage_block=None, **options):
        BUILD.mkdir(parents=True, exist_ok=True)
        data = pack(raw, **options)
        if damage_block is not None:
            data = damaged(data, opened(data).blocks[damage_block].offset + 36 + 20)
        path = BUILD / name
        path.write_bytes(data)
        return path

    def test_video_from_a_capture(self):
        raw = run_of_frames(180, 524)
        path = self.capture("video.sscap", raw)
        from_raw = record.record(io.BytesIO(raw), BUILD / "video_raw.mp4", log=lambda m: None)
        from_capture = record.record_capture(path, BUILD / "video_sscap.mp4", log=lambda m: None)
        self.assertEqual((from_capture.frames, from_capture.end_seen), (from_raw.frames, True))
        self.assertEqual(from_capture.frames, 180)
        done = subprocess.run([sys.executable, str(ROOT / "record.py"), str(path),
                               str(BUILD / "video_cli.mp4")], capture_output=True, text=True,
                              cwd=ROOT)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("180 frames", done.stderr)

    def test_video_of_a_clip(self):
        raw = run_of_frames(180, 524)
        path = self.capture("video_clip.sscap", raw)
        result = record.record_capture(path, BUILD / "video_clip.mp4", start=1.0, end=2.0,
                                       log=lambda m: None)
        self.assertEqual((result.frames, result.end_seen), (60, False))
        with self.assertRaisesRegex(record.RecordError, "Can't cut that clip"):
            record.record_capture(path, BUILD / "never.mp4", start=5, log=lambda m: None)

        def cli(*args):
            return subprocess.run([sys.executable, str(ROOT / "record.py"), *map(str, args)],
                                  capture_output=True, text=True, cwd=ROOT)

        done = cli(path, BUILD / "video_clip_cli.mp4", "--from", "0:01.5")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Clip from 0:01.50 to 0:03.00", done.stderr)
        self.assertIn("90 frames", done.stderr)
        (BUILD / "video_clip.raw").write_bytes(raw)
        done = cli(BUILD / "video_clip.raw", BUILD / "never.mp4", "--to", "1")
        self.assertEqual(done.returncode, 2)
        self.assertIn("cut a clip from a capture", done.stderr)

    def test_a_damaged_block_is_skipped(self):
        notes = []
        path = self.capture("video_damaged.sscap", run_of_frames(180, 524), damage_block=1)
        result = record.record_capture(path, BUILD / "video_damaged.mp4", log=notes.append)
        self.assertEqual(result.frames, 120)
        self.assertTrue(any("Skipped a damaged part" in n and "block 1" in n for n in notes))

    def test_recovery_copies_rescue_a_damaged_block_0(self):
        raw = run_of_frames(180, 524)
        path = self.capture("video_block0.sscap", raw, damage_block=0)
        result = record.record_capture(path, BUILD / "video_block0.mp4", log=lambda m: None)
        self.assertEqual(result.frames, 120, "HEAD and PALT come from the recovery copies")
        path = self.capture("video_block0_bare.sscap", raw, damage_block=0, recovery=False)
        with self.assertRaisesRegex(record.RecordError, "No frames"):
            record.record_capture(path, BUILD / "video_block0_bare.mp4", log=lambda m: None)

    def test_not_a_capture(self):
        BUILD.mkdir(parents=True, exist_ok=True)
        path = BUILD / "not_a_capture.sscap"
        path.write_bytes(b"\x89SSC\r\n\x1a\n" + bytes(8))
        with self.assertRaisesRegex(record.RecordError, "Can't read the capture"):
            record.record_capture(path, BUILD / "never.mp4", log=lambda m: None)


@unittest.skipUnless(rawstream.find_stella(), "needs a Stella with -rawstream")
class LiveCapture(unittest.TestCase):
    def test_record_the_test_rom(self):
        rom = testrom.write(BUILD / "stellastream_test.bin")
        proc = launch_stella(rom)
        safety = threading.Timer(30, close_stella, (proc,))
        safety.start()
        raw = bytearray()
        out = io.BytesIO()
        closer = None
        try:
            with Writer(out) as w:
                while data := proc.stdout.read(1 << 16):
                    raw += data
                    w.write(data)
                    if w.frames >= 130 and closer is None:
                        # Ask Stella to close and read on, so it sends END
                        closer = threading.Thread(target=close_stella, args=(proc,))
                        closer.start()
        finally:
            safety.cancel()
            proc.stdout.close()
            if closer:
                closer.join()
            close_stella(proc)
        cap = opened(out.getvalue())
        self.assertEqual(cap.stream().read(), bytes(raw), "a byte-for-byte round trip")
        self.assertGreaterEqual(len(cap.blocks), 3)
        self.assertTrue(cap.blocks[0].flags & HAS_HEAD and cap.blocks[0].flags & HAS_PALT)
        self.assertEqual(cap.blocks[1].frames, 60, "the test ROM's frames are all 262 lines")
        self.assertTrue(cap.blocks[-1].flags & HAS_END, "Stella closed normally")
        self.assertGreater(len(raw) / len(out.getvalue()), 50)

    def test_record_rom_capture_alone_needs_no_ffmpeg(self):
        rom = testrom.write(BUILD / "stellastream_test.bin")
        path = BUILD / "rom_capture.sscap"
        path.unlink(missing_ok=True)
        result = record.record_rom(rom, capture=path, sound=False, seconds=2,
                                   ffmpeg="no-such-ffmpeg", log=lambda m: None)
        self.assertEqual((result.path, result.capture), (path, path))
        self.assertAlmostEqual(result.seconds, 2, delta=0.05)
        with open(path, "rb") as f:
            cap = Capture(f)
            self.assertTrue(cap.indexed)
            self.assertEqual(cap.frames, result.frames)
            frames = sum(isinstance(i, rawstream.Frame) for i in parse(cap.stream()))
        self.assertEqual(frames, result.frames)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_record_rom_video_and_capture_together(self):
        rom = testrom.write(BUILD / "stellastream_test.bin")
        video, path = BUILD / "rom_both.mp4", BUILD / "rom_both.sscap"
        for old in (video, path):
            old.unlink(missing_ok=True)
        result = record.record_rom(rom, video, capture=path, sound=False, seconds=2,
                                   log=lambda m: None)
        self.assertEqual((result.path, result.capture, result.frames), (video, path, 120))
        self.assertTrue(video.is_file())
        with open(path, "rb") as f:
            cap = Capture(f)
            self.assertIn(cap.frames, (120, 121), "the stream as far as the video took it")
        from_capture = record.record_capture(path, BUILD / "rom_both_again.mp4",
                                             log=lambda m: None)
        self.assertEqual(from_capture.frames, 120)


class NoStream(unittest.TestCase):
    def test_no_stream_leaves_no_capture(self):
        rom = testrom.write(BUILD / "stellastream_test.bin")
        path = BUILD / "never.sscap"
        path.unlink(missing_ok=True)

        def silent_stella(*args, **kwargs):  # a Stella that ends without a stream
            return subprocess.Popen([sys.executable, "-c", "pass"], stdout=subprocess.PIPE,
                                    bufsize=0)

        with mock.patch("record.launch_stella", silent_stella):
            with self.assertRaisesRegex(record.RecordError, "closed without sending a stream"):
                record.record_rom(rom, capture=path, log=lambda m: None)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
