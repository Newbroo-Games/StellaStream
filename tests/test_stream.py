"""Tests that need no Stella: the reader, the checker, the RAM watch and the
recording engine, all on streams built here. The engine tests need ffmpeg.

    python -m unittest discover -s tests -t .
"""
import io
import shutil
import struct
import threading
import unittest
from pathlib import Path

import rawstream
from rawstream import Audio, End, Frame, Header, Jump, Palette, Ram, parse
import record

BUILD = Path(__file__).resolve().parent.parent / "build" / "tests"
MONO = [i * 1000 for i in range(31)]
STEREO = [i * 2000 for i in range(16)]


def chunk(tag, payload=b""):
    return tag.encode("ascii") + struct.pack("<I", len(payload)) + payload


def head(timing=0, rate=31440, name=b"synthetic.bin"):
    return chunk("HEAD", struct.pack("<HBBIIHH", 1, timing, 0, rate, rate * 38, 160, 0)
                 + struct.pack("<31h", *MONO) + struct.pack("<16h", *STEREO)
                 + struct.pack("<H", len(name)) + name + struct.pack("<H", 3) + b"abc")


def palette():
    return chunk("PALT", bytes(i % 256 for i in range(768)))


def audio(samples, value=0x21):
    return chunk("AUDI", bytes([value]) * samples)


def frame(number, color=0x1E, height=4, lines=262, cycles=0):
    return chunk("FRAM", struct.pack("<QIHHHH", cycles, number, lines, 23, 160, height)
                 + bytes([color]) * (160 * height))


def run_of_frames(count, samples_each, lines=262):
    """HEAD, PALT, then count frames each after samples_each samples."""
    data = head() + palette()
    for n in range(count):
        data += audio(samples_each) + frame(n + 1, lines=lines, cycles=(n + 1) * lines * 76)
    return data + chunk("END ")


class Reader(unittest.TestCase):
    def test_chunks_in_order_and_unknown_skipped(self):
        data = (head() + palette() + audio(524) + chunk("RAM ", bytes(range(128)))
                + frame(1) + chunk("ZZZZ", b"later chunk") + chunk("JUMP", b"\x01")
                + chunk("END "))
        items = list(parse(io.BytesIO(data)))
        self.assertEqual([type(i) for i in items], [Header, Palette, Audio, Ram, Frame, Jump, End])
        h, _, a, ram, f, j, _ = items
        self.assertEqual((h.sample_rate, h.timing_name, h.rom_name, h.rom_md5),
                         (31440, "NTSC", "synthetic.bin", "abc"))
        self.assertEqual((h.mono, h.stereo), (tuple(MONO), tuple(STEREO)))
        self.assertEqual(h.samples_per_frame, 524)
        self.assertEqual(len(a.samples), 524)
        self.assertEqual((ram[0x80], ram[0xFF]), (0, 127))
        self.assertEqual((f.number, f.scanlines, f.start_line, f.height, len(f.pixels)),
                         (1, 262, 23, 4, 640))
        self.assertEqual(j.name, "state load")

    def test_cut_off_stream_ends_quietly(self):
        data = head() + frame(1)[:100]
        self.assertEqual([type(i) for i in parse(io.BytesIO(data))], [Header])

    def test_watch_list(self):
        symbols = {"Frame": 0xDC}
        self.assertEqual(rawstream.watch_list("Frame, $b6,0x1b7", symbols),
                         [("Frame", 0xDC), ("$b6", 0xB6), ("0x1b7", 0xB7)])
        with self.assertRaises(ValueError):
            rawstream.watch_list("$7f")
        with self.assertRaises(ValueError):
            rawstream.watch_list("NoSuchName", symbols)

    def test_checker(self):
        out = io.StringIO()
        self.assertTrue(rawstream.report(io.BytesIO(run_of_frames(5, 524)), out=out))
        # One frame with a sample too many must fail the sync check
        bad = head() + palette()
        for n, samples in enumerate((524, 524, 525, 524)):
            bad += audio(samples) + frame(n + 1, cycles=(n + 1) * 262 * 76)
        self.assertFalse(rawstream.report(io.BytesIO(bad), out=io.StringIO()))


class Sound(unittest.TestCase):
    def test_mono_and_stereo_mix(self):
        h = next(parse(io.BytesIO(head())))
        # 0x21: channel 0 at volume 1, channel 1 at volume 2
        self.assertEqual(record.to_pcm(b"\x21", record.audio_tables(h, False)),
                         struct.pack("<h", MONO[3]))
        self.assertEqual(record.to_pcm(b"\x21", record.audio_tables(h, True)),
                         struct.pack("<hh", STEREO[1], STEREO[2]))


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class Engine(unittest.TestCase):
    def record(self, data, name, **kw):
        BUILD.mkdir(parents=True, exist_ok=True)
        return record.record(io.BytesIO(data), BUILD / name, log=lambda m: None, **kw)

    def test_sound_is_the_clock(self):
        # Frames twice as long as nominal are written twice, half as long every other one
        long = self.record(run_of_frames(10, 1048, lines=524), "long.mp4")
        self.assertEqual(long.frames, 20)
        short = self.record(run_of_frames(20, 262, lines=131), "short.mp4")
        self.assertEqual(short.frames, 10)
        self.assertTrue(long.end_seen)
        self.assertTrue(long.path.is_file())
        self.assertFalse((BUILD / "long.video.mkv").exists(), "temporary files are removed")

    def test_seconds_limit_and_progress(self):
        calls = []
        result = self.record(run_of_frames(120, 524), "limit.mp4", seconds=1,
                             progress=lambda f, s, b: calls.append((f, s, b)))
        self.assertEqual(result.frames, 60)
        self.assertAlmostEqual(result.seconds, 1.0)
        self.assertEqual(calls[-1][:2], (60, 1.0))
        self.assertGreater(calls[-1][2], 0)

    def test_stop_event(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaisesRegex(record.RecordError, "No frames"):
            self.record(run_of_frames(10, 524), "stopped.mp4", stop=stop)

    def test_missing_ffmpeg(self):
        with self.assertRaisesRegex(record.RecordError, "ffmpeg"):
            record.check_ffmpeg("no-such-ffmpeg-here")


if __name__ == "__main__":
    unittest.main()
