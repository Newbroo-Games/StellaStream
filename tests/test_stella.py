"""Tests that run Stella (silently, on its own settings folder) on the test ROM
from testrom.py. They need a Stella with -rawstream (found as the app finds it)
and ffmpeg; without them they are skipped.

    python -m unittest discover -s tests -t .
"""
import collections
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import rawstream
from rawstream import Frame, Ram, close_stella, launch_stella, parse
import record
from tests import testrom

BUILD = Path(__file__).resolve().parent.parent / "build" / "tests"
ROM = BUILD / "stellastream_test.bin"
HAVE_STELLA = rawstream.find_stella() is not None
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


def setUpModule():
    testrom.write(ROM)


def quiet(message):
    pass


@unittest.skipUnless(HAVE_STELLA, "needs a Stella with -rawstream")
class LiveStream(unittest.TestCase):
    def test_ram_matches_the_picture(self):
        proc = launch_stella(ROM)
        try:
            pairs = []
            ram = None
            for item in parse(proc.stdout):
                if isinstance(item, Ram):
                    ram = item
                elif isinstance(item, Frame):
                    pairs.append((ram, item))
                    if len(pairs) == 90:
                        break
        finally:
            proc.stdout.close()
            close_stella(proc)
        counts = [r[testrom.COUNTER] for r, _ in pairs[1:]]
        self.assertEqual([(b - a) % 256 for a, b in zip(counts, counts[1:])],
                         [1] * (len(counts) - 1), "the counter steps by 1 a frame")
        for r, f in pairs[1:]:
            colour = collections.Counter(f.pixels).most_common(1)[0][0]
            self.assertEqual(colour, r[testrom.COUNTER] & 0xFE,
                             f"frame {f.number}: background follows RAM $80")
        self.assertTrue(all(f.scanlines == 262 for _, f in pairs[1:]))


@unittest.skipUnless(HAVE_STELLA and HAVE_FFMPEG, "needs a Stella with -rawstream and ffmpeg")
class RecordRom(unittest.TestCase):
    def test_seconds_limit(self):
        out = BUILD / "rom_2s.mp4"
        result = record.record_rom(ROM, out, sound=False, seconds=2, log=quiet)
        self.assertEqual(result.frames, 120)
        self.assertTrue(out.is_file())
        if shutil.which("ffprobe"):
            streams = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,width,height",
                 "-of", "csv=p=0", str(out)], capture_output=True, text=True).stdout.split()
            self.assertEqual(sorted(streams), ["aac", "h264,640,456"])

    @unittest.skipUnless(shutil.which("ffprobe"), "needs ffprobe")
    def test_sync_on_263_lines(self):
        rom = testrom.write(BUILD / "stellastream_test_263.bin", lines=263)
        for sync, rate in (("picture", "15720/263"), ("smooth", "60/1")):
            out = BUILD / f"rom_263_{sync}.mp4"
            result = record.record_rom(rom, out, sound=False, seconds=2, sync=sync, log=quiet)
            self.assertEqual(result.frames, 120, sync)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
                 "-show_entries", "stream=r_frame_rate,nb_read_packets", "-of", "csv=p=0",
                 str(out)], capture_output=True, text=True).stdout.strip()
            self.assertEqual(probe, f"{rate},120", sync)

    def test_stop_closes_stella_politely(self):
        stop = threading.Event()
        frames = []
        outcome = {}

        def run():
            try:
                outcome["result"] = record.record_rom(
                    ROM, BUILD / "rom_stopped.mp4", sound=False, stop=stop, log=quiet,
                    progress=lambda f, s, b: frames.append(f))
            except record.RecordError as e:
                outcome["error"] = e

        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 30
        while (not frames or frames[-1] < 60) and time.monotonic() < deadline:
            time.sleep(0.1)
        stop.set()
        worker.join(30)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", outcome)
        self.assertGreaterEqual(outcome["result"].frames, 60)
        self.assertTrue(outcome["result"].end_seen, "Stella closed normally and sent END")


class Failures(unittest.TestCase):
    """Each failure must end quickly with a message for the user."""

    @staticmethod
    def fake_stella(code):
        def launch(*args, **kwargs):
            return subprocess.Popen([sys.executable, "-c", code],
                                    stdout=subprocess.PIPE, bufsize=0)
        return launch

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_stella_that_never_streams(self):
        start = time.monotonic()
        with mock.patch("record.launch_stella", self.fake_stella("import time; time.sleep(30)")):
            with self.assertRaisesRegex(record.RecordError, "no stream in 1 s"):
                record.record_rom(ROM, BUILD / "never.mp4", head_timeout=1, log=quiet)
        self.assertLess(time.monotonic() - start, 10)

    @unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
    def test_stella_that_closes_without_a_stream(self):
        with mock.patch("record.launch_stella", self.fake_stella("pass")):
            with self.assertRaisesRegex(record.RecordError, "closed without sending a stream"):
                record.record_rom(ROM, BUILD / "closed.mp4", log=quiet)

    def test_missing_rom(self):
        with self.assertRaisesRegex(record.RecordError, "ROM file isn't there"):
            record.record_rom(BUILD / "no_such_game.bin", BUILD / "x.mp4", log=quiet)


if __name__ == "__main__":
    unittest.main()
