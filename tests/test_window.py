"""Tests for the window (stellastream.pyw): the settings file, the Stella
options the fields make, the checks before recording, and the window itself,
built hidden on a settings file of its own and closed again. No dialog opens.
WindowRecords records the test ROM from the window, with Stella silent on the
app's own settings folder; it needs Stella and ffmpeg. WindowMakesVideos makes
videos from a capture built here; it needs ffmpeg.

    python -m unittest discover -s tests -t .
"""
import importlib.machinery
import importlib.util
import json
import sys
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path

import rawstream
import sscap
from tests import testrom
from tests.test_stella import BUILD, HAVE_FFMPEG, HAVE_STELLA, ROM
from tests.test_stream import run_of_frames

APP_FILE = Path(__file__).resolve().parent.parent / "stellastream.pyw"


def write_capture(path, frames=180):
    """A capture of the synthetic stream: frames NTSC frames (180: 3 s)."""
    with open(path, "wb") as f, sscap.Writer(f) as w:
        w.write(run_of_frames(frames, 524))
    return path


def load_app():
    """Import stellastream.pyw (a .pyw isn't importable by name everywhere)."""
    loader = importlib.machinery.SourceFileLoader("stellastream", str(APP_FILE))
    spec = importlib.util.spec_from_loader("stellastream", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


app = load_app()


def fields(**changes):
    s = dict(app.DEFAULTS)
    s.update(changes)
    return s


def destroy(root):
    """Destroy a window and what it left to run later: Tcl would run that while
    the next test's window runs, for a window that is gone."""
    try:
        for pending in root.tk.splitlist(root.tk.call("after", "info")):
            root.after_cancel(pending)
        root.destroy()
    except tk.TclError:
        pass  # closed by the test


def closed(root):
    try:
        return not root.winfo_exists()
    except tk.TclError:
        return True


class SettingsFile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "StellaStream" / "settings.json"

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip(self):
        s = fields(rom="game.bin", left="DRIVING", format="PAL", scale=2, stereo=True,
                   hear=False, time_limit=True, seconds=90, crf=18, extra="-tv.filter 1")
        app.save_settings(self.path, s)
        self.assertEqual(app.load_settings(self.path), s)

    def test_missing_file_gives_defaults(self):
        self.assertEqual(app.load_settings(self.path), app.DEFAULTS)

    def test_broken_file_gives_defaults(self):
        self.path.parent.mkdir(parents=True)
        for text in ("not json", "[1, 2]", ""):
            self.path.write_text(text, encoding="utf-8")
            self.assertEqual(app.load_settings(self.path), app.DEFAULTS)

    def test_bad_values_keep_their_defaults(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "rom": 5, "left": "NOT_A_CONTROLLER", "format": "ntsc", "scale": 3,
            "hear": "yes", "seconds": 0, "crf": True, "stereo": True, "unknown": 1}),
            encoding="utf-8")
        self.assertEqual(app.load_settings(self.path), fields(stereo=True))


class StellaOptions(unittest.TestCase):
    def test_auto_sends_nothing(self):
        self.assertEqual(app.stella_options(fields()), [])

    def test_controllers_and_format(self):
        s = fields(left="DRIVING", right="PADDLES", format="PAL60")
        self.assertEqual(app.stella_options(s),
                         ["-lc", "DRIVING", "-rc", "PADDLES", "-format", "PAL60"])

    def test_extra_options_keep_quotes_and_backslashes(self):
        s = fields(extra=r'-lc Driving -snapsavedir "C:\My Videos\snaps"')
        self.assertEqual(app.stella_options(s),
                         ["-lc", "Driving", "-snapsavedir", r"C:\My Videos\snaps"])

    def test_open_quote(self):
        with self.assertRaises(ValueError):
            app.stella_options(fields(extra='-snapsavedir "C:\\x'))

    def test_command_is_what_launch_stella_runs(self):
        s = fields(rom="game.bin", left="DRIVING", hear=False, stella="Stella.exe")
        self.assertEqual(
            rawstream.stella_command("game.bin", ["-lc", "DRIVING"], silent=True,
                                     own_settings=False, stella="Stella.exe"),
            ["Stella.exe", "-rawstream", "-", "-audio.enabled", "0", "-lc", "DRIVING",
             "game.bin"])
        self.assertEqual(app.command_line(s),
                         "Stella.exe -rawstream - -audio.enabled 0 -lc DRIVING game.bin")

    def test_sound_on_and_placeholders(self):
        command = app.command_line(fields())
        self.assertNotIn("-audio.enabled", command)
        self.assertNotIn("-basedir", command)  # the user's own Stella settings
        self.assertIn("<Stella>", command)
        self.assertIn("<ROM>", command)

    def test_default_video(self):
        self.assertEqual(app.default_video(str(Path("roms") / "game.bin")),
                         str(Path("roms") / "game.mp4"))
        self.assertEqual(app.default_video(""), "")


class BeforeRecording(unittest.TestCase):
    """The checks and names worked out before Stella starts."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.folder = Path(self.dir.name)
        self.rom = self.folder / "game.bin"
        self.rom.write_bytes(bytes(4096))

    def tearDown(self):
        self.dir.cleanup()

    def test_free_name(self):
        video = self.folder / "game.mp4"
        self.assertEqual(app.free_name(video), video)
        video.touch()
        self.assertEqual(app.free_name(video), self.folder / "game-2.mp4")
        (self.folder / "game-2.audio.wav").touch()  # kept from an earlier recording
        self.assertEqual(app.free_name(video), self.folder / "game-3.mp4")

    def test_free_names_keep_one_number(self):
        video, capture = self.folder / "game.mp4", self.folder / "game.sscap"
        numbered = lambda n: (self.folder / f"game-{n}.mp4", self.folder / f"game-{n}.sscap")
        self.assertEqual(app.free_names(video, capture), (video, capture))
        video.touch()
        self.assertEqual(app.free_names(video, capture), numbered(2))
        numbered(2)[1].touch()  # only the capture of that name is there
        self.assertEqual(app.free_names(video, capture), numbered(3))
        self.assertEqual(app.free_names(None, capture), (None, capture))
        capture.touch()
        self.assertEqual(app.free_names(None, capture), (None, numbered(3)[1]))

    def test_there_already(self):
        video, capture = self.folder / "game.mp4", self.folder / "game.sscap"
        self.assertEqual(app.there_already(video, capture), [])
        capture.touch()
        self.assertEqual(app.there_already(video, capture), [capture])
        self.assertEqual(app.there_already(video), [])
        video.touch()
        (self.folder / "game.audio.wav").touch()
        self.assertEqual(app.there_already(video, capture),
                         [video, self.folder / "game.audio.wav", capture])

    def test_a_bare_video_name_goes_next_to_the_rom(self):
        s = fields(rom=str(self.rom), video="mine.mp4")
        self.assertEqual(app.video_path(s), self.folder / "mine.mp4")
        s = fields(rom=str(self.rom), video=str(self.folder / "sub" / "mine.mp4"))
        self.assertEqual(app.video_path(s), self.folder / "sub" / "mine.mp4")

    def test_each_problem_says_what_to_fix(self):
        good = dict(rom=str(self.rom), video=str(self.folder / "out" / "game.mp4"),
                    stella=sys.executable)  # stands in for a Stella that is there
        for changes, message in (
                (dict(rom=""), "Choose the game's ROM file"),
                (dict(rom=str(self.folder / "gone.bin")), "ROM file isn't there"),
                (dict(video=" "), "Choose where to save the video"),
                (dict(video="game.avi"), "must end in .mp4"),
                (dict(video="game.gif"), "A GIF is made from a capture: save a Capture"),
                (dict(stella=str(self.folder / "no_stella.exe")), "Stella isn't there"),
                (dict(extra='-x "open'), "a quote isn't closed")):
            s = fields(**dict(good, **changes))
            self.assertIn(message, app.check_fields(s, found_stella=None), changes)
        self.assertIn("No Stella found", app.check_fields(fields(**dict(good, stella=""))))
        self.assertIsNone(app.check_fields(fields(**dict(good, stella="")), "Stella.exe"))
        self.assertIsNone(app.check_fields(fields(**good)))
        self.assertTrue((self.folder / "out").is_dir(), "the video's folder is made")

    def test_capture_problems(self):
        good = dict(rom=str(self.rom), stella=sys.executable, save="CAPTURE", video="",
                    capture=str(self.folder / "caps" / "game.sscap"))
        self.assertIsNone(app.check_fields(fields(**good)), "a capture alone needs no video")
        self.assertTrue((self.folder / "caps").is_dir(), "the capture's folder is made")
        for changes, message in ((dict(capture=" "), "Choose where to save the capture"),
                                 (dict(capture="game.cap"), "must end in .sscap"),
                                 (dict(save="BOTH"), "Choose where to save the video")):
            self.assertIn(message, app.check_fields(fields(**dict(good, **changes))), changes)
        self.assertIsNone(app.check_fields(fields(**dict(good, save="VIDEO", video="game.mp4",
                                                         capture="game.cap"))),
                          "a capture that isn't saved isn't checked")

    def test_video_from_a_capture(self):
        capture = write_capture(self.folder / "game.sscap")
        self.assertEqual(app.default_clip_video(str(Path("caps") / "game.sscap")),
                         str(Path("caps") / "game-clip.mp4"))
        self.assertEqual(app.default_clip_video(str(Path("caps") / "game.sscap"), ".gif"),
                         str(Path("caps") / "game-clip.gif"))
        self.assertEqual(app.default_clip_video(" "), "")
        self.assertEqual(app.clip_video_path(fields(clip_capture=str(capture),
                                                    clip_video="mine.mp4")),
                         self.folder / "mine.mp4", "a bare name goes next to the capture")
        self.assertEqual(app.capture_note(str(capture)), "0:03.00 long, 180 frames")
        self.assertEqual(app.capture_note(str(self.folder / "gone.sscap")), "")
        self.assertTrue(app.capture_note(str(self.rom)).startswith(
            "Can't read it: not a StellaStream capture"))

    def test_clip_times(self):
        for start, end, times in (("", "", (0.0, None)), ("1:20", "1:45", (80, 105)),
                                  (" 2.5 ", "", (2.5, None)), ("", "3", (0.0, 3))):
            self.assertEqual(app.clip_times(fields(clip_from=start, clip_to=end)), times)
        for start, end, message in (("x", "", "From: give a time"), ("", "1:99", "To: give a time"),
                                    ("1:20", "1:20", "To must be after From")):
            with self.assertRaisesRegex(ValueError, message):
                app.clip_times(fields(clip_from=start, clip_to=end))

    def test_each_clip_problem_says_what_to_fix(self):
        capture = write_capture(self.folder / "game.sscap")
        good = dict(clip_capture=str(capture), clip_video=str(self.folder / "out" / "clip.mp4"))
        for changes, message in (
                (dict(clip_capture=""), "Choose the capture"),
                (dict(clip_capture=str(self.folder / "gone.sscap")), "capture file isn't there"),
                (dict(clip_capture=str(self.rom)), "Can't read the capture"),
                (dict(clip_from="x"), "From: give a time"),
                (dict(clip_from="3"), "at or after the capture's end (0:03.00)"),
                (dict(clip_from="2", clip_to="1"), "To must be after From"),
                (dict(clip_video=" "), "Choose where to save the video"),
                (dict(clip_video="clip.avi"), "must end in .mp4")):
            self.assertIn(message, app.check_clip_fields(fields(**dict(good, **changes))), changes)
        self.assertIsNone(app.check_clip_fields(fields(**good)))
        self.assertTrue((self.folder / "out").is_dir(), "the video's folder is made")
        self.assertIsNone(app.check_clip_fields(fields(**dict(good, clip_video="clip.GIF"))))

    def test_clock(self):
        self.assertEqual((app.clock(42.9), app.clock(600), app.clock(3723)),
                         ("00:42", "10:00", "1:02:03"))

    def test_reveal_command(self):
        video = Path("videos") / "game.mp4"
        self.assertEqual(app.reveal_command(video, "win32"), f'explorer /select,"{video}"')
        self.assertEqual(app.reveal_command(video, "darwin"), ["open", "-R", str(video)])
        self.assertEqual(app.reveal_command(video, "linux"), ["xdg-open", "videos"])


class Window(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as e:
            self.skipTest(f"no display for a window: {e}")
        self.root.withdraw()
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "settings.json"
        self.window = app.App(self.root, settings_file=self.path)
        self.window.found_stella = "Stella.exe"

    def tearDown(self):
        destroy(self.root)
        self.dir.cleanup()

    def command(self):
        return self.window.command.get("1.0", "end-1c")

    def test_command_follows_the_fields(self):
        v = self.window.vars
        rom = str(Path("roms") / "game.bin")
        v["rom"].set(rom)
        v["left"].set("Driving")
        v["format"].set("PAL")
        v["hear"].set(False)
        self.assertTrue(self.command().startswith(
            "Stella.exe -rawstream - -audio.enabled 0 -lc DRIVING -format PAL"))
        self.assertTrue(self.command().endswith(rom))
        v["extra"].set('-x "open')
        self.assertIn("quote isn't closed", self.command())

    def test_video_follows_the_rom_until_named(self):
        v = self.window.vars
        v["rom"].set(str(Path("roms") / "one.bin"))
        self.assertEqual(v["video"].get(), str(Path("roms") / "one.mp4"))
        v["rom"].set(str(Path("roms") / "two.bin"))
        self.assertEqual(v["video"].get(), str(Path("roms") / "two.mp4"))
        v["video"].set("mine.mp4")
        v["rom"].set(str(Path("roms") / "three.bin"))
        self.assertEqual(v["video"].get(), "mine.mp4")

    def test_capture_follows_the_video_until_named(self):
        v = self.window.vars
        v["rom"].set(str(Path("roms") / "one.bin"))
        self.assertEqual(v["capture"].get(), str(Path("roms") / "one.sscap"))
        v["video"].set("mine.mp4")
        self.assertEqual(v["capture"].get(), "mine.sscap")
        v["capture"].set("keep.sscap")
        v["video"].set("other.mp4")
        self.assertEqual(v["capture"].get(), "keep.sscap")

    def test_the_file_not_saved_is_greyed(self):
        w = self.window

        def greyed(widgets):
            return [widget.instate(["disabled"]) for widget in widgets]

        self.assertTrue(all(greyed(w.for_capture)) and not any(greyed(w.for_video)))
        w.vars["save"].set("Capture")
        self.assertTrue(all(greyed(w.for_video)) and not any(greyed(w.for_capture)))
        w.vars["save"].set("Both")
        self.assertFalse(any(greyed(w.for_video + w.for_capture)))
        w.vars["save"].set("Video")
        w._lock(True)
        self.assertTrue(all(greyed(w.for_video + w.for_capture)), "all locked while recording")
        self.assertTrue(w.make_button.instate(["disabled"]))
        w._lock(False)
        self.assertTrue(all(greyed(w.for_capture)) and not any(greyed(w.for_video)))

    def test_a_bad_number_keeps_the_last_good_one(self):
        v = self.window.vars
        v["seconds"].set("45")
        v["seconds"].set("4x")
        v["crf"].set("99")
        s = self.window.settings()
        self.assertEqual((s["seconds"], s["crf"]), (45, 12))

    def test_more_options_fold(self):
        v = self.window.vars
        self.assertFalse(self.window.more.winfo_manager())
        self.assertIn("More options", self.window.fold.cget("text"))
        v["more"].set(True)
        self.assertEqual(self.window.more.winfo_manager(), "grid")
        self.window.fold.invoke()
        self.assertFalse(v["more"].get())
        self.assertFalse(self.window.more.winfo_manager())

    def test_video_from_a_capture_fold(self):
        w = self.window
        self.assertFalse(w.clip_box.winfo_manager())
        self.assertEqual(w.clip_fold.cget("text"), "▸ Video or GIF from a capture")
        w.clip_fold.invoke()
        self.assertEqual(w.clip_box.winfo_manager(), "grid")
        self.assertEqual(w.clip_fold.cget("text"), "▾ Video or GIF from a capture")

    def test_clip_video_follows_the_capture_until_named(self):
        v = self.window.vars
        v["clip_capture"].set(str(Path("caps") / "one.sscap"))
        self.assertEqual(v["clip_video"].get(), str(Path("caps") / "one-clip.mp4"))
        v["clip_video"].set(str(Path("caps") / "one-clip.gif"))
        v["clip_capture"].set(str(Path("caps") / "two.sscap"))
        self.assertEqual(v["clip_video"].get(), str(Path("caps") / "two-clip.gif"), "still a GIF")
        v["clip_video"].set("mine.mp4")
        v["clip_capture"].set(str(Path("caps") / "three.sscap"))
        self.assertEqual(v["clip_video"].get(), "mine.mp4")

    def test_the_button_says_video_or_gif(self):
        w = self.window
        self.assertEqual(w.make_button.cget("text"), "Make video")
        self.assertIn("name it .gif for an animated GIF", w.make_note.get())
        w.vars["clip_video"].set("clip.gif")
        self.assertEqual(w.make_button.cget("text"), "Make GIF")
        self.assertIn("640 wide, 50 frames a second, no sound", w.make_note.get())

    def test_make_video_says_what_to_fix_and_starts_nothing(self):
        self.window.make_button.invoke()
        self.assertEqual(self.window.status.get(), "Choose the capture to make a video or GIF from.")
        self.assertIsNone(self.window.job)

    def test_closing_saves_and_a_new_window_remembers(self):
        v = self.window.vars
        v["rom"].set("game.bin")
        v["right"].set("Paddles")
        v["scale"].set("2")
        v["time_limit"].set(True)
        v["more"].set(True)
        v["save"].set("Both")
        self.window.close()
        saved = app.load_settings(self.path)
        self.assertEqual((saved["rom"], saved["video"], saved["right"], saved["scale"],
                          saved["time_limit"], saved["more"]),
                         ("game.bin", "game.mp4", "PADDLES", 2, True, True))
        self.assertEqual((saved["save"], saved["capture"]), ("BOTH", "game.sscap"))
        self.root = tk.Tk()
        self.root.withdraw()
        again = app.App(self.root, settings_file=self.path)
        self.assertEqual((again.vars["right"].get(), again.vars["scale"].get()),
                         ("Paddles", "2"))

    def test_record_says_what_to_fix_and_starts_nothing(self):
        self.window.record_button.invoke()
        self.assertEqual(self.window.status.get(), "Choose the game's ROM file.")
        self.assertIsNone(self.window.job)
        v = self.window.vars
        v["time_limit"].set(True)
        v["seconds"].set("4x")
        self.window.record_button.invoke()
        self.assertIn("Stop after: give a whole number", self.window.status.get())
        self.assertIsNone(self.window.job)
        self.assertFalse(self.window.inputs[0].instate(["disabled"]))


class RunningWindow(unittest.TestCase):
    """A window that runs jobs: setUp() makes it (self.root, self.window)."""

    def setUp(self):
        for old in BUILD.glob("window*.*"):
            old.unlink()
        self.root = tk.Tk()
        self.root.withdraw()
        self.dir = tempfile.TemporaryDirectory()
        self.window = app.App(self.root, settings_file=Path(self.dir.name) / "settings.json")

    def tearDown(self):
        job = self.window.job
        if job:  # a test failed mid-recording: don't leave Stella running
            job.stop.set()
            job.thread.join(30)
        destroy(self.root)
        self.dir.cleanup()

    def run_window(self, until, timeout=40):
        """Run the window until until() is true. True if the window closed instead."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if closed(self.root):
                return True
            self.root.update()
            if until():
                return False
            time.sleep(0.02)
        self.fail(f"still waiting after {timeout} s; the status says: {self.window.status.get()}")


@unittest.skipUnless(HAVE_STELLA and HAVE_FFMPEG, "needs a Stella with -rawstream and ffmpeg")
class WindowRecords(RunningWindow):
    """Record the test ROM from the window, as a user would, but silently and
    on the app's own Stella settings."""

    @classmethod
    def setUpClass(cls):
        testrom.write(ROM)

    def setUp(self):
        super().setUp()
        self.window.own_settings = True
        v = self.window.vars
        v["rom"].set(str(ROM))
        v["video"].set(str(BUILD / "window.mp4"))
        v["hear"].set(False)

    def recording(self, frames=30):
        """Record, and wait until the video has this many frames."""
        w = self.window
        w.record_button.invoke()
        self.run_window(lambda: w.job is None or (w.job.progress or (0,))[0] >= frames)
        self.assertIsNotNone(w.job, w.status.get())

    def test_time_limit_then_a_new_name(self):
        w = self.window
        w.vars["time_limit"].set(True)
        w.vars["seconds"].set("1")
        for name in ("window.mp4", "window-2.mp4"):
            w.record_button.invoke()
            self.assertTrue(w.inputs[0].instate(["disabled"]), "the fields are locked")
            self.assertTrue(w.record_button.instate(["disabled"]))
            self.assertFalse(w.stop_button.instate(["disabled"]))
            self.run_window(lambda: w.job is None)
            self.assertTrue(w.status.get().startswith("Saved"), w.status.get())
            self.assertIn("00:01, 60 frames", w.status.get())
            self.assertEqual(w.last_video, BUILD / name)
            self.assertTrue((BUILD / name).is_file())
            self.assertIn("Picture 160x", w.note.get())
            self.assertFalse(w.inputs[0].instate(["disabled"]), "the fields are free again")
            self.assertFalse(w.open_button.instate(["disabled"]))
            self.assertFalse(w.folder_button.instate(["disabled"]))
        self.assertIn("(window.mp4 was there already)", w.status.get())

    def test_capture_alone_then_both(self):
        w = self.window
        w.vars["time_limit"].set(True)
        w.vars["seconds"].set("1")
        w.vars["save"].set("Capture")
        w.record_button.invoke()
        self.run_window(lambda: w.job is None)
        self.assertTrue(w.status.get().startswith(f"Saved {BUILD / 'window.sscap'}: 00:01"),
                        w.status.get())
        self.assertTrue((BUILD / "window.sscap").is_file())
        self.assertFalse((BUILD / "window.mp4").exists())
        self.assertTrue(w.open_button.instate(["disabled"]), "a capture has no player to open")
        self.assertFalse(w.folder_button.instate(["disabled"]))
        w.vars["save"].set("Both")
        (BUILD / "window.mp4").touch()  # so both names are taken
        w.record_button.invoke()
        self.run_window(lambda: w.job is None)
        self.assertIn("window-2.mp4 and window-2.sscap (window.mp4 and window.sscap were there "
                      "already): 00:01, 60 frames", w.status.get())
        self.assertFalse(w.open_button.instate(["disabled"]))
        for name in ("window-2.mp4", "window-2.sscap"):
            self.assertTrue((BUILD / name).is_file(), name)

    def test_stop(self):
        w = self.window
        self.recording()
        self.assertTrue(w.status.get().startswith("Recording..."), w.status.get())
        w.stop_button.invoke()
        self.assertTrue(w.stop_button.instate(["disabled"]))
        self.assertTrue(w.status.get().startswith("Stopping"), w.status.get())
        self.run_window(lambda: w.job is None)
        self.assertTrue(w.status.get().startswith("Saved"), w.status.get())
        self.assertTrue((BUILD / "window.mp4").is_file())

    def test_closing_saves_the_video_first(self):
        w = self.window
        self.recording()
        w.close()
        self.assertIsNotNone(w.job, "still recording: the window waits for the video")
        self.assertEqual(w.status.get(), "Saving the video, then closing...")
        self.assertTrue(self.run_window(lambda: False), "the window closes by itself")
        self.assertTrue((BUILD / "window.mp4").is_file())
        self.assertEqual(app.load_settings(w.settings_file)["rom"], str(ROM))


@unittest.skipUnless(HAVE_FFMPEG, "needs ffmpeg")
class WindowMakesVideos(RunningWindow):
    """Make videos from a capture in the window, as a user would."""

    def setUp(self):
        super().setUp()
        BUILD.mkdir(parents=True, exist_ok=True)
        self.capture = write_capture(BUILD / "window_clip.sscap")
        self.window.vars["clip_capture"].set(str(self.capture))

    def test_a_clip_then_a_new_name(self):
        w, v = self.window, self.window.vars
        self.assertEqual(v["clip_video"].get(), str(BUILD / "window_clip-clip.mp4"))
        self.assertEqual(w.clip_note.get(), "0:03.00 long, 180 frames")
        v["clip_from"].set("1")
        v["clip_to"].set("0:02")
        for name in ("window_clip-clip.mp4", "window_clip-clip-2.mp4"):
            w.make_button.invoke()
            self.assertTrue(w.make_button.instate(["disabled"]), "locked while it works")
            self.assertTrue(w.record_button.instate(["disabled"]))
            self.assertFalse(w.stop_button.instate(["disabled"]))
            self.run_window(lambda: w.job is None)
            self.assertTrue(w.status.get().startswith(f"Saved {BUILD / name}"), w.status.get())
            self.assertIn("00:01, 60 frames", w.status.get())
            self.assertIn("Clip from 0:01.00 to 0:02.00", w.note.get())
            self.assertEqual(w.last_video, BUILD / name)
            self.assertFalse(w.open_button.instate(["disabled"]))
            self.assertFalse(w.make_button.instate(["disabled"]))
        self.assertIn("(window_clip-clip.mp4 was there already)", w.status.get())

    def test_the_whole_capture(self):
        w = self.window
        w.make_button.invoke()
        self.run_window(lambda: w.job is None)
        self.assertIn("00:03, 180 frames", w.status.get())
        self.assertNotIn("Clip from", w.note.get())

    def test_a_failure_says_so(self):
        w = self.window
        w.vars["ffmpeg"].set("no_such_ffmpeg")
        w.make_button.invoke()
        self.run_window(lambda: w.job is None)
        self.assertTrue(w.status.get().startswith("Making the video failed. ffmpeg doesn't run"),
                        w.status.get())


class WindowMakesGifs(RunningWindow):
    """Make a GIF from a capture in the window: no ffmpeg needed."""

    def test_a_gif_of_a_clip(self):
        w, v = self.window, self.window.vars
        BUILD.mkdir(parents=True, exist_ok=True)
        v["clip_capture"].set(str(write_capture(BUILD / "window_gif.sscap")))
        v["clip_video"].set(str(BUILD / "window_gif-clip.gif"))
        v["ffmpeg"].set("no_such_ffmpeg")
        v["clip_from"].set("1")
        v["clip_to"].set("0:02")
        w.make_button.invoke()
        self.assertTrue(w.make_button.instate(["disabled"]), "locked while it works")
        self.run_window(lambda: w.job is None)
        self.assertTrue(w.status.get().startswith(f"Saved {BUILD / 'window_gif-clip.gif'}: "
                                                  "00:01, 50 frames"), w.status.get())
        self.assertIn("GIF 640x8, 60 fps shown at 50", w.note.get())
        self.assertEqual(w.last_video, BUILD / "window_gif-clip.gif")
        self.assertFalse(w.open_button.instate(["disabled"]))
        self.assertEqual(app.Job(video=BUILD / "x.gif", asked=None).what, "the GIF")


if __name__ == "__main__":
    unittest.main()
