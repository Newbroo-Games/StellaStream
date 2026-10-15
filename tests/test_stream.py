"""Tests that need no Stella: the reader, the checker, the RAM watch and the
recording engine, all on streams built here. The engine tests need ffmpeg.

    python -m unittest discover -s tests -t .
"""
import io
import shutil
import struct
import subprocess
import threading
import unittest
import wave
from fractions import Fraction
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


MODE_2 = (0, 4, 10, 1, 7, 3, 9, 0, 5, 8, 2, 6, 10, 1, 4, 6)  # extra lines, like flicker.asm's mode 2


def run_sync(method, lines):
    """Feed one sync method frames of the given line counts, each with a whole
    frame of sound whose every sample is its frame number (mod 256); returns
    (the method, [(frame, times)] in order, the sound given out)."""
    h = next(parse(io.BytesIO(head())))
    sync = record.SYNC_METHODS[method](h)
    shown, sound = [], bytearray()
    for k, n in enumerate(lines):
        ready, out = sync.frame(k, bytes([k % 256]) * (2 * n), n)
        shown += ready
        sound += out
    ready, out = sync.finish()
    return sync, shown + ready, bytes(sound + out)


class SyncMethods(unittest.TestCase):
    """The three ways of keeping picture and sound together, without ffmpeg."""

    def test_all_alike_on_262_lines(self):
        lines = [262] * 100
        _, _, want = run_sync("sound", lines)
        for method in record.SYNCS:
            sync, shown, sound = run_sync(method, lines)
            self.assertEqual(sync.fps, 60, method)
            self.assertEqual(shown, [(k, 1) for k in range(100)], method)
            self.assertEqual(sound, want, method)
        self.assertEqual(want, b"".join(bytes([k]) * 524 for k in range(100)))

    def test_sound_repeats_a_picture_to_keep_up(self):
        sync, shown, sound = run_sync("sound", [263] * 600)
        self.assertEqual(sum(t for _, t in shown), round(600 * 526 / 524))
        self.assertEqual(sorted({t for _, t in shown}), [1, 2])
        self.assertEqual(len(sound), 600 * 526, "every sample kept")

    def test_sound_shows_a_short_first_frame(self):
        _, shown, _ = run_sync("sound", [258 + e for e in MODE_2] * 4)
        self.assertGreaterEqual(shown[0][1], 1)

    def test_picture_at_the_games_own_rate(self):
        sync, shown, sound = run_sync("picture", [263] * 100)
        self.assertEqual(sync.fps, Fraction(31440, 526))
        self.assertEqual(shown, [(k, 1) for k in range(100)])
        self.assertEqual(sound, b"".join(bytes([k]) * 526 for k in range(100)),
                         "a steady length keeps every sample")

    def test_picture_cuts_and_pads_a_changing_length(self):
        lines = [258, 266] * 60  # 262 on average
        sync, shown, sound = run_sync("picture", lines)
        self.assertEqual(sync.fps, 60)
        self.assertEqual(len(shown), 120)
        self.assertEqual(sound[:524], bytes([0]) * 524, "516 samples padded with the last")
        self.assertEqual(sound[524:1048], bytes([1]) * 524, "532 samples cut")

    def test_picture_sets_its_rate_from_a_short_stream(self):
        sync, shown, sound = run_sync("picture", [272] * 10)
        self.assertEqual(sync.fps, Fraction(31440, 544))
        self.assertEqual((len(shown), len(sound)), (10, 10 * 544))

    def test_smooth_keeps_each_frames_sound_under_it(self):
        sync, shown, sound = run_sync("smooth", [263] * 200)
        self.assertEqual(sync.fps, 60)
        self.assertEqual(shown, [(k, 1) for k in range(200)])
        self.assertEqual(sound, b"".join(bytes([k]) * 524 for k in range(200)),
                         "a steady length is stretched evenly, frame by frame")

    def test_smooth_eases_a_changing_length(self):
        lines = [258 + e for e in MODE_2] * 20
        _, shown, sound = run_sync("smooth", lines)
        self.assertEqual(len(shown), len(lines))
        self.assertEqual(len(sound), len(lines) * 524)
        for k in range(len(lines)):
            part = sound[k * 524:(k + 1) * 524]
            near = {k % 256, (k - 1) % 256, (k + 1) % 256}
            self.assertTrue(set(part) <= near, f"frame {k}: only its own and its neighbours' sound")
            self.assertGreater(part.count(k % 256), 0.95 * 524, f"frame {k}: mostly its own")

    def test_smooth_gives_sound_a_little_behind_and_keeps_little(self):
        h = next(parse(io.BytesIO(head())))
        sync = record.SYNC_METHODS["smooth"](h)
        given = 0
        for k in range(3000):
            _, out = sync.frame(k, bytes(2 * (258 + MODE_2[k % 16])), 258 + MODE_2[k % 16])
            given += len(out)
            if k >= record.EASE:
                self.assertEqual(given, (k + 1 - record.EASE) * 524)
        self.assertLess(len(sync.starts), 1100)
        self.assertLess(len(sync.sound), 40 * 524)
        given += len(sync.finish()[1])
        self.assertEqual(given, 3000 * 524)


def probe(path):
    """ffprobe: the video's rate, then each video packet's duration."""
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=r_frame_rate:packet=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.split()
    return out[-1], out[:-1]


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class Engine(unittest.TestCase):
    def record(self, data, name, **kw):
        BUILD.mkdir(parents=True, exist_ok=True)
        return record.record(io.BytesIO(data), BUILD / name, log=lambda m: None, **kw)

    def test_sound_is_the_clock(self):
        # Frames twice as long as nominal are written twice, half as long every other one
        long = self.record(run_of_frames(10, 1048, lines=524), "long.mp4", sync="sound")
        self.assertEqual(long.frames, 20)
        short = self.record(run_of_frames(20, 262, lines=131), "short.mp4", sync="sound")
        self.assertEqual(short.frames, 10)
        self.assertTrue(long.end_seen)
        self.assertTrue(long.path.is_file())
        self.assertFalse(any(p.exists() for p in record.side_files(long.path)),
                         "temporary files are removed")

    @unittest.skipUnless(shutil.which("ffprobe"), "needs ffprobe")
    def test_sync_rates_and_exact_times(self):
        data = run_of_frames(300, 526, lines=263)
        for sync, rate, pictures in (("smooth", "60/1", 300), ("picture", "15720/263", 300),
                                     ("sound", "60/1", 301)):
            result = self.record(data, f"sync_{sync}.mp4", sync=sync)
            self.assertEqual(result.frames, pictures, sync)
            got_rate, durations = probe(result.path)
            self.assertEqual(got_rate, rate, sync)
            self.assertEqual(len(durations), pictures, sync)
            self.assertEqual(len(set(durations)), 1, f"{sync}: every picture as long")
        self.assertAlmostEqual(result.seconds, 301 / 60)

    def test_a_cut_first_frame_is_left_out(self):
        # The stream starts inside a frame: 39 lines, with less than its sound
        data = head() + palette() + audio(70) + frame(1, lines=39)
        for n in range(60):
            data += audio(524) + frame(n + 2)
        for sync in record.SYNCS:
            result = self.record(data, f"cut_{sync}.mp4", sync=sync, keep=True)
            video, sound = record.side_files(result.path)
            with wave.open(str(sound)) as w:
                self.assertEqual((result.frames, w.getnframes()), (60, 60 * 524), sync)
            video.unlink()
            sound.unlink()

    def test_unknown_sync(self):
        with self.assertRaisesRegex(ValueError, "smooth, picture, sound"):
            self.record(run_of_frames(2, 524), "bad.mp4", sync="audio")

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
