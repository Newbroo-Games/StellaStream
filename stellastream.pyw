"""StellaStream: record Atari 2600 games played in Stella to MP4 videos.

The window is a front end for record.py. Fill in the game and the video; the
window shows the Stella command it will run, and remembers every field in a
settings file of the user's own (settings_path()). Record runs
record.record_rom() in a worker thread, which reports through a queue the
window reads every POLL_MS. Under "Video or GIF from a capture", Make video
runs record.record_capture() the same way, on the part of a capture between
two times; a name ending in .gif makes an animated GIF (gif.py).

    python stellastream.pyw
"""
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import filedialog, ttk

import rawstream
import record
import sscap

# (shown, sent to Stella); Auto sends nothing and lets Stella decide
CONTROLLERS = [
    ("Auto", "AUTO"), ("Joystick", "JOYSTICK"), ("Paddles", "PADDLES"),
    ("Driving", "DRIVING"), ("Keyboard", "KEYBOARD"), ("Booster Grip", "BOOSTERGRIP"),
    ("Sega Genesis", "GENESIS"), ("Trak-Ball", "TRAKBALL"), ("Atari mouse", "ATARIMOUSE"),
    ("Amiga mouse", "AMIGAMOUSE"), ("Light Gun", "LIGHTGUN"), ("MindLink", "MINDLINK"),
    ("Kid Vid", "KIDVID"), ("CompuMate", "COMPUMATE"), ("SaveKey", "SAVEKEY"),
    ("AtariVox", "ATARIVOX"), ("QuadTari", "QUADTARI"), ("Joy 2B+", "JOY_2B+"),
]
TV_FORMATS = [("Auto", "AUTO"), ("NTSC", "NTSC"), ("PAL", "PAL"), ("SECAM", "SECAM"),
              ("NTSC50", "NTSC50"), ("PAL60", "PAL60"), ("SECAM60", "SECAM60")]
SAVE_MODES = [("Video", "VIDEO"), ("Capture", "CAPTURE"), ("Both", "BOTH")]
CHOICES = {"left": CONTROLLERS, "right": CONTROLLERS, "format": TV_FORMATS, "save": SAVE_MODES}

DEFAULTS = {
    "rom": "", "left": "AUTO", "right": "AUTO", "format": "AUTO",
    "save": "VIDEO", "video": "", "capture": "", "scale": 1, "stereo": False, "hear": True,
    "time_limit": False, "seconds": 60,
    "more": False, "stella": "", "ffmpeg": "ffmpeg", "extra": "", "crf": 12, "keep": False,
    "clip": False, "clip_capture": "", "clip_from": "", "clip_to": "", "clip_video": "",
}
RANGES = {"scale": (1, 2), "seconds": (1, 24 * 3600), "crf": (0, 51)}
VIDEO_TYPES = (".mp4", ".mkv", ".mov")  # files ffmpeg puts H.264 and AAC in

LABEL_WIDTH = 20  # characters; lines the fields up across the boxes
PADY = 3
POLL_MS = 200  # how often the window reads what the recording said


def settings_path():
    """settings.json in %APPDATA%/StellaStream on Windows, in
    ~/.config/StellaStream elsewhere."""
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "StellaStream" / "settings.json"


def _valid(key, value):
    default = DEFAULTS[key]
    if type(value) is not type(default):
        return False
    if key in CHOICES:
        return value in dict(CHOICES[key]).values()
    if key in RANGES:
        low, high = RANGES[key]
        return low <= value <= high
    return True


def load_settings(path):
    """The saved fields over the defaults. Anything missing, unreadable or of
    the wrong kind keeps its default."""
    settings = dict(DEFAULTS)
    try:
        saved = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings
    if isinstance(saved, dict):
        settings.update((k, v) for k, v in saved.items() if k in DEFAULTS and _valid(k, v))
    return settings


def save_settings(path, settings):
    """Write the settings; a half-written file never replaces a good one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(temp, path)


def split_options(text):
    """Extra Stella options as a list: spaces separate, quotes group, and
    backslashes stay as they are (Windows paths). ValueError on an open quote."""
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    lexer.commenters = ""
    return list(lexer)


def stella_options(s):
    """The Stella options the fields ask for, from settings s (before the ROM)."""
    args = []
    for key, option in (("left", "-lc"), ("right", "-rc"), ("format", "-format")):
        if s[key] != "AUTO":
            args += [option, s[key]]
    return args + split_options(s["extra"])


def command_line(s, found_stella=None):
    """The Stella command the fields make, as text to show. The user plays,
    so Stella keeps the user's own settings. ValueError on an open quote."""
    args = rawstream.stella_command(s["rom"] or "<ROM>", stella_options(s),
                                    silent=not s["hear"], own_settings=False,
                                    stella=s["stella"] or found_stella or "<Stella>")
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def default_video(rom):
    """The video's name when the user hasn't chosen one: the ROM's, as .mp4."""
    rom = Path(rom.strip())
    return str(rom.with_suffix(".mp4")) if rom.name else ""


def default_capture(video):
    """The capture's name when the user hasn't chosen one: the video's, as .sscap."""
    video = Path(video.strip())
    return str(video.with_suffix(".sscap")) if video.name else ""


def default_clip_video(capture, suffix=".mp4"):
    """The name of a video (or, with suffix .gif, a GIF) made from a capture
    when the user hasn't chosen one: the capture's, as -clip.mp4."""
    capture = Path(capture.strip())
    return str(capture.with_name(capture.stem + "-clip" + suffix)) if capture.name else ""


def _beside(s, key, base="rom"):
    """The path in field key; a name without a folder, or a relative path,
    counts from the folder of the file in field base (the ROM's)."""
    path = Path(s[key].strip())
    return path if path.is_absolute() else Path(s[base].strip()).absolute().parent / path


def video_path(s):
    """Where settings s save the video."""
    return _beside(s, "video")


def capture_path(s):
    """Where settings s save the capture."""
    return _beside(s, "capture")


def clip_video_path(s):
    """Where settings s save the video made from a capture: next to the
    capture unless the name has a folder."""
    return _beside(s, "clip_video", base="clip_capture")


def clip_times(s):
    """(start, end) in seconds from the From and To fields; an empty From is
    the start (0), an empty To the end (None). ValueError says what to fix."""
    times = []
    for key, name in (("clip_from", "From"), ("clip_to", "To")):
        text = s[key].strip()
        try:
            times.append(sscap.parse_time(text) if text else None)
        except ValueError:
            raise ValueError(f"{name}: give a time such as 80, 1:20 or 1:20.5 "
                             "(minutes:seconds).") from None
    start, end = times
    if start is not None and end is not None and end <= start:
        raise ValueError("To must be after From.")
    return start or 0.0, end


def capture_length(path):
    """(seconds, frames) of a capture file. Raises CaptureError or OSError."""
    with open(path, "rb") as f:
        cap = sscap.Capture(f)
        return cap.seconds(), cap.frames


def capture_note(path):
    """The line under the capture field: its length, or why it can't be read;
    empty while there is no file of that name."""
    path = path.strip()
    if not path or not Path(path).is_file():
        return ""
    try:
        seconds, frames = capture_length(path)
    except (sscap.CaptureError, OSError) as e:
        return f"Can't read it: {e}"
    return f"{sscap.time_text(seconds)} long, {frames:,} frames"


def _numbered(path, n):
    path = Path(path)
    return path if n == 1 else path.with_name(f"{path.stem}-{n}{path.suffix}")


def free_names(video=None, capture=None):
    """(video, capture), or the first game-2, game-3, ... that is free for
    both, so the two keep the same number: a recording never replaces a
    file, nor record()'s separate picture and sound files. None for a file
    not saved."""
    n = 1
    while True:
        names = tuple(_numbered(p, n) if p else None for p in (video, capture))
        if not there_already(*names):
            return names
        n += 1


def there_already(video=None, capture=None):
    """The files a recording to video and capture would replace: those two
    and record()'s separate picture and sound files, the ones that exist."""
    files = ([video, *record.side_files(video)] if video else []) + [capture]
    return [p for p in files if p and p.exists()]


def free_name(video):
    """free_names() for a video alone."""
    return free_names(video)[0]


def check_fields(s, found_stella=None):
    """The first thing in settings s that stops a recording, as a message
    saying what to fix; None if nothing does. To check that the video's
    folder can be written to, this makes it if it isn't there."""
    rom = s["rom"].strip()
    if not rom:
        return "Choose the game's ROM file."
    if not Path(rom).is_file():
        return f"The ROM file isn't there: {rom}"
    targets = []  # (what, path) for each file saved
    if s["save"] != "CAPTURE":
        if not s["video"].strip():
            return "Choose where to save the video."
        video = video_path(s)
        if record.is_gif(video):
            return ("A GIF is made from a capture: save a Capture (or Both), then make the "
                    "GIF under Video or GIF from a capture.")
        if video.suffix.lower() not in VIDEO_TYPES:
            return "The video's name must end in .mp4 (or .mkv or .mov)."
        targets.append(("video", video))
    if s["save"] != "VIDEO":
        if not s["capture"].strip():
            return "Choose where to save the capture."
        capture = capture_path(s)
        if capture.suffix.lower() != ".sscap":
            return "The capture's name must end in .sscap."
        targets.append(("capture", capture))
    stella = s["stella"].strip()
    if stella and not (Path(stella).is_file() or shutil.which(stella)):
        return (f"Stella isn't there: {stella}. Choose it under More options, or empty "
                "that field to use the one found automatically.")
    if not (stella or found_stella):
        return "No Stella found. Choose a Stella with -rawstream under More options."
    try:
        split_options(s["extra"])
    except ValueError:
        return "Extra Stella options: a quote isn't closed."
    for what, path in targets:
        problem = _cant_save(what, path)
        if problem:
            return problem
    return None


def _cant_save(what, path):
    """Why a file can't be saved at path, or None; makes its folder if it isn't there."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path.parent):
            pass
    except OSError as e:
        return f"Can't save the {what} in {path.parent}: {e.strerror or e}"
    return None


def check_clip_fields(s):
    """The first thing in settings s that stops making a video or a GIF from
    a capture, as a message saying what to fix; None if nothing does. Like
    check_fields(), this makes the video's folder if it isn't there."""
    capture = s["clip_capture"].strip()
    if not capture:
        return "Choose the capture to make a video or GIF from."
    if not Path(capture).is_file():
        return f"The capture file isn't there: {capture}"
    try:
        seconds, _ = capture_length(capture)
    except (sscap.CaptureError, OSError) as e:
        return f"Can't read the capture: {e}."
    try:
        start, _ = clip_times(s)
    except ValueError as e:
        return str(e)
    if start >= seconds:
        return f"From is at or after the capture's end ({sscap.time_text(seconds)})."
    if not s["clip_video"].strip():
        return "Choose where to save the video or GIF."
    video = clip_video_path(s)
    if record.is_gif(video):
        return _cant_save("GIF", video)
    if video.suffix.lower() not in VIDEO_TYPES:
        return "The name must end in .mp4 or .gif (or .mkv or .mov)."
    return _cant_save("video", video)


def clock(seconds):
    """00:42, or 1:02:03 from an hour on."""
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def reveal_command(path, platform=sys.platform):
    """The command that shows a file in its folder: selected, in Explorer or
    the Finder; elsewhere the folder opens."""
    if platform == "win32":
        return f'explorer /select,"{path}"'  # Explorer wants the quotes after the comma
    if platform == "darwin":
        return ["open", "-R", str(path)]
    return ["xdg-open", str(Path(path).parent)]


def open_file(path):
    """Open a file in the program the system has for it."""
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


@dataclass
class Job:
    """A recording, or a video being made from a capture, under way: its
    worker thread and what it shares with the window."""
    video: Path   # the video it is saved to; None when only a capture is saved
    asked: Path   # the video the fields name; video differs when that one was there already
    capture: Path = None        # the capture it is saved to, if any
    asked_capture: Path = None  # the capture the fields name
    source: Path = None         # the capture a video is made from (None: recording)
    there: list = field(default_factory=list)  # the files named that were there already
    thread: threading.Thread = None
    messages: queue.Queue = field(default_factory=queue.Queue)
    stop: threading.Event = field(default_factory=threading.Event)
    finishing: threading.Event = field(default_factory=threading.Event)
    progress: tuple = None  # the latest (frames, seconds, bytes)
    notes: list = field(default_factory=list)

    @property
    def what(self):
        """'the video', 'the GIF', 'the capture' or 'the files', for the status line."""
        if self.video and self.capture:
            return "the files"
        if self.video:
            return "the GIF" if record.is_gif(self.video) else "the video"
        return "the capture"


def work_in_thread(messages, work, **kwargs):
    """The worker thread: run work(**kwargs) (record.record_rom or
    record.record_capture) and put everything it says on messages, since only
    the main thread may touch the window."""
    try:
        result = work(progress=lambda *p: messages.put(("progress", p)),
                      log=lambda note: messages.put(("note", note)), **kwargs)
        messages.put(("done", result))
    except (record.RecordError, OSError) as e:
        messages.put(("error", str(e)))
    except Exception as e:  # a bug: say so, and give the window back
        messages.put(("error", f"Something went wrong inside StellaStream: {e!r}"))


class App:
    def __init__(self, root, settings_file=None):
        self.root = root
        self.settings_file = Path(settings_file) if settings_file else settings_path()
        self.found_stella = rawstream.find_stella()
        s = load_settings(self.settings_file)
        if not s["capture"].strip():
            s["capture"] = default_capture(s["video"])
        if not s["clip_video"].strip():
            s["clip_video"] = default_clip_video(s["clip_capture"])
        self._good = dict(s)  # last valid value of each number field
        self._save_job = None
        self._folds = {}       # key: (button, frame, title) of each fold
        self._fold_shown = {}  # key: shown or not, as last drawn
        self._last_rom = s["rom"]
        self._last_video = s["video"]
        self._last_clip_capture = s["clip_capture"]
        self._wrap = None
        self.inputs = []          # what is locked while recording
        self.job = None           # the recording under way
        self.closing = False      # the window closes once the recording is saved
        self.last_video = None    # what Open video and Show folder show
        self.own_settings = False  # tests set it: Stella then uses the app's own settings

        self.vars = {}
        for key, value in s.items():
            if key in CHOICES:
                var = tk.StringVar(value=dict((v, k) for k, v in CHOICES[key])[value])
            elif isinstance(value, bool):
                var = tk.BooleanVar(value=value)
            else:
                var = tk.StringVar(value=str(value))  # numbers too: typed freely
            self.vars[key] = var

        self._build()
        self.vars["rom"].trace_add("write", self._rom_changed)
        self.vars["video"].trace_add("write", self._video_changed)
        self.vars["clip_capture"].trace_add("write", self._clip_capture_changed)
        self.vars["clip_video"].trace_add("write", self._clip_video_changed)
        self.clip_note.set(capture_note(s["clip_capture"]))
        self._clip_video_changed()
        for var in self.vars.values():
            var.trace_add("write", self._changed)
        self._update()
        root.protocol("WM_DELETE_WINDOW", self.close)

    # --- layout ---

    def _build(self):
        root, v = self.root, self.vars
        root.title("StellaStream")
        root.columnconfigure(0, weight=1)
        root.resizable(True, False)
        ttk.Style(root).configure("Note.TLabel", foreground="#555555")
        main = ttk.Frame(root, padding=12)
        main.grid(sticky="nsew")
        main.columnconfigure(0, weight=1)

        game = self._box(main, 0, "Game")
        self._file_row(game, 0, "ROM file", "rom", self._browse_rom)
        self._label(game, 1, "Left control")
        controls = ttk.Frame(game)
        controls.grid(row=1, column=1, columnspan=2, sticky="w", pady=PADY)
        self._combo(controls, "left", CONTROLLERS).pack(side="left")
        ttk.Label(controls, text="Right control").pack(side="left", padx=(24, 8))
        self._combo(controls, "right", CONTROLLERS).pack(side="left")
        self._label(game, 2, "TV format")
        self._combo(game, "format", TV_FORMATS).grid(row=2, column=1, sticky="w", pady=PADY)

        video = self._box(main, 1, "Save")
        self._label(video, 0, "Save")
        modes = ttk.Frame(video)
        modes.grid(row=0, column=1, columnspan=2, sticky="w", pady=PADY)
        for i, (shown, _) in enumerate(SAVE_MODES):
            self._input(ttk.Radiobutton(modes, text=shown, variable=v["save"], value=shown)).pack(
                side="left", padx=(0 if i == 0 else 16, 0))
        # Fields that only matter for one of the two files: greyed when it isn't saved
        self.for_video = self._file_row(video, 1, "Video file", "video", self._browse_video)
        self.for_capture = self._file_row(video, 2, "Capture file", "capture",
                                          self._browse_capture)
        self._label(video, 3, "Size")
        sizes = ttk.Frame(video)
        sizes.grid(row=3, column=1, columnspan=2, sticky="w", pady=PADY)
        self.for_video += [
            self._input(ttk.Radiobutton(sizes, text="640 x 456", variable=v["scale"], value="1")),
            self._input(ttk.Radiobutton(sizes, text="1280 x 912", variable=v["scale"],
                                        value="2"))]
        self.for_video[-2].pack(side="left")
        self.for_video[-1].pack(side="left", padx=(16, 0))
        ttk.Label(sizes, text="(PAL games: 640 x 548 and 1280 x 1096)",
                  style="Note.TLabel").pack(side="left", padx=(16, 0))
        sound = ttk.Frame(video)
        sound.grid(row=4, column=0, columnspan=3, sticky="w", pady=PADY)
        self.for_video.append(self._input(ttk.Checkbutton(sound, text="Stereo sound",
                                                          variable=v["stereo"])))
        self.for_video[-1].pack(side="left")
        self._input(ttk.Checkbutton(sound, text="Hear the game while recording",
                                    variable=v["hear"])).pack(side="left", padx=(24, 0))
        ttk.Label(sound, text="(the files have the sound either way)",
                  style="Note.TLabel").pack(side="left", padx=(6, 0))
        limit = ttk.Frame(video)
        limit.grid(row=5, column=0, columnspan=3, sticky="w", pady=PADY)
        self._input(ttk.Checkbutton(limit, text="Stop after",
                                    variable=v["time_limit"])).pack(side="left")
        self._input(ttk.Spinbox(limit, from_=10, to=RANGES["seconds"][1], increment=10, width=6,
                                textvariable=v["seconds"])).pack(side="left", padx=6)
        ttk.Label(limit, text="seconds").pack(side="left")

        self.fold, more = self._fold(main, 2, "more", "More options")
        self.more = more
        self._file_row(more, 0, "Stella program", "stella",
                       lambda: self._browse_program("stella", "Choose Stella"))
        found = ("Empty: the one found automatically (see the command below)"
                 if self.found_stella else "Empty: none found yet, so choose one")
        ttk.Label(more, text=found, style="Note.TLabel").grid(row=1, column=1, columnspan=2, sticky="w")
        self._file_row(more, 2, "ffmpeg program", "ffmpeg",
                       lambda: self._browse_program("ffmpeg", "Choose ffmpeg"))
        self._label(more, 3, "Extra Stella options")
        self._input(ttk.Entry(more, textvariable=v["extra"])).grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=PADY)
        quality = ttk.Frame(more)
        quality.grid(row=4, column=0, columnspan=3, sticky="w", pady=PADY)
        ttk.Label(quality, text="Quality (lower is better)").pack(side="left")
        self.for_video += [
            self._input(ttk.Spinbox(quality, from_=0, to=51, width=4, textvariable=v["crf"])),
            self._input(ttk.Checkbutton(quality, text="Keep the separate picture and sound files",
                                        variable=v["keep"]))]
        self.for_video[-2].pack(side="left", padx=(6, 24))
        self.for_video[-1].pack(side="left")

        box = self._box(main, 4, "Stella command (what the app runs; it can be copied)")
        box.columnconfigure(0, weight=1)
        box.columnconfigure(1, weight=0)
        self.command = tk.Text(box, height=3, width=72, wrap="word", font="TkFixedFont",
                               relief="flat", padx=4, pady=4, state="disabled")
        self.command.grid(row=0, column=0, sticky="ew")
        self.command.bind("<Configure>", lambda e: self._fit_command())
        ttk.Button(box, text="Copy", command=self._copy_command).grid(
            row=0, column=1, sticky="n", padx=(8, 0))

        # A video or a GIF of a capture, or of the part of it between two times
        self.clip_fold, clip = self._fold(main, 5, "clip", "Video or GIF from a capture")
        self.clip_box = clip
        self._file_row(clip, 0, "Capture file", "clip_capture", self._browse_clip_capture)
        self.clip_note = tk.StringVar()
        ttk.Label(clip, textvariable=self.clip_note, style="Note.TLabel").grid(
            row=1, column=1, columnspan=2, sticky="w")
        self._label(clip, 2, "From")
        times = ttk.Frame(clip)
        times.grid(row=2, column=1, columnspan=2, sticky="w", pady=PADY)
        self._input(ttk.Entry(times, textvariable=v["clip_from"], width=10)).pack(side="left")
        ttk.Label(times, text="To").pack(side="left", padx=(16, 8))
        self._input(ttk.Entry(times, textvariable=v["clip_to"], width=10)).pack(side="left")
        ttk.Label(times, text="(minutes:seconds; empty: the start, the end)",
                  style="Note.TLabel").pack(side="left", padx=(16, 0))
        self._file_row(clip, 3, "Video or GIF file", "clip_video", self._browse_clip_video)
        make = ttk.Frame(clip)
        make.grid(row=4, column=0, columnspan=3, sticky="w", pady=(PADY, 0))
        self.make_button = ttk.Button(make, command=self.make_video)  # its text: _clip_video_changed()
        self.make_button.pack(side="left")
        self.make_note = tk.StringVar()
        ttk.Label(make, textvariable=self.make_note, style="Note.TLabel").pack(
            side="left", padx=(16, 0))

        bar = ttk.Frame(main)
        bar.grid(row=7, sticky="ew", pady=(12, 0))
        self.record_button = ttk.Button(bar, text="● Record", command=self.record)
        self.record_button.pack(side="left")
        self.stop_button = ttk.Button(bar, text="■ Stop", state="disabled", command=self.stop)
        self.stop_button.pack(side="left", padx=(8, 0))
        self.folder_button = ttk.Button(bar, text="Show folder", state="disabled",
                                        command=self.show_folder)
        self.folder_button.pack(side="right")
        self.open_button = ttk.Button(bar, text="Open video", state="disabled",
                                      command=self.open_video)
        self.open_button.pack(side="right", padx=(0, 8))
        # What is happening, and Stella's notes on the recording under it
        self.status = tk.StringVar(value="Ready to record.")
        self.note = tk.StringVar()
        self.status_label = ttk.Label(main, textvariable=self.status, justify="left")
        self.status_label.grid(row=8, sticky="ew", pady=(8, 0))
        self.note_label = ttk.Label(main, textvariable=self.note, justify="left",
                                    style="Note.TLabel")
        self.note_label.grid(row=9, sticky="ew")
        self.status_label.bind("<Configure>", lambda e: self._wrap_status(e.width))

    def _box(self, parent, row, title):
        box = ttk.LabelFrame(parent, text=title, padding=(10, 4, 10, 6))
        box.grid(row=row, sticky="ew", pady=(0 if row == 0 else 10, 0))
        box.columnconfigure(1, weight=1)
        return box

    def _fold(self, parent, row, key, title):
        """A button that shows or hides a frame of fields (field key: shown);
        returns the two. The frame's columns line up with the boxes'."""
        button = ttk.Button(parent, style="Toolbutton",
                            command=lambda: self.vars[key].set(not self.vars[key].get()))
        button.grid(row=row, sticky="w", pady=(8, 0))
        frame = ttk.Frame(parent, padding=(10, 0, 10, 4))
        frame.grid(row=row + 1, sticky="ew")
        frame.columnconfigure(1, weight=1)
        self._folds[key] = (button, frame, title)
        return button, frame

    def _label(self, parent, row, text):
        ttk.Label(parent, text=text, width=LABEL_WIDTH).grid(row=row, column=0, sticky="w")

    def _input(self, widget):
        """A field or a Browse button: locked while recording."""
        self.inputs.append(widget)
        return widget

    def _file_row(self, parent, row, text, key, browse):
        """A label, a path field and its Browse button; returns the two inputs."""
        self._label(parent, row, text)
        entry = self._input(ttk.Entry(parent, textvariable=self.vars[key]))
        entry.grid(row=row, column=1, sticky="ew", pady=PADY)
        button = self._input(ttk.Button(parent, text="Browse...", command=browse))
        button.grid(row=row, column=2, padx=(8, 0))
        return [entry, button]

    def _combo(self, parent, key, choices):
        return self._input(ttk.Combobox(parent, textvariable=self.vars[key], state="readonly",
                                        width=14, values=[shown for shown, _ in choices]))

    def _wrap_status(self, width):
        """Wrap the status lines at the window's width; a long message would
        otherwise widen the window past the screen."""
        if width > 100 and width != self._wrap:
            self._wrap = width
            self.status_label.configure(wraplength=width)
            self.note_label.configure(wraplength=width)
            self.root.after_idle(self._fit_window)

    def _say(self, text):
        self.status.set(text)
        self.root.after_idle(self._fit_window)

    # --- fields ---

    def settings(self):
        """The fields as settings. A number field that isn't a usable number
        counts as its last good value."""
        s = {}
        for key, var in self.vars.items():
            value = var.get()
            if key in CHOICES:
                value = dict(CHOICES[key]).get(value, DEFAULTS[key])
            elif key in RANGES:
                try:
                    value = int(value.strip())
                except ValueError:
                    value = None
                if value is None or not _valid(key, value):
                    value = self._good[key]
                self._good[key] = value
            s[key] = value
        return s

    def _rom_changed(self, *_):
        # The video follows the ROM until the user names it something else
        rom = self.vars["rom"].get()
        video = self.vars["video"].get()
        if not video.strip() or video == default_video(self._last_rom):
            self.vars["video"].set(default_video(rom))
        self._last_rom = rom

    def _video_changed(self, *_):
        # And the capture follows the video the same way
        video = self.vars["video"].get()
        capture = self.vars["capture"].get()
        if not capture.strip() or capture == default_capture(self._last_video):
            self.vars["capture"].set(default_capture(video))
        self._last_video = video

    def _clip_capture_changed(self, *_):
        # The video (or GIF) made from a capture follows it the same way; the
        # note says how long it is
        capture = self.vars["clip_capture"].get()
        video = self.vars["clip_video"].get()
        suffix = ".gif" if record.is_gif(video.strip()) else ".mp4"
        if not video.strip() or video == default_clip_video(self._last_clip_capture, suffix):
            self.vars["clip_video"].set(default_clip_video(capture, suffix))
        self._last_clip_capture = capture
        self.clip_note.set(capture_note(capture))
        self.root.after_idle(self._fit_window)

    def _clip_video_changed(self, *_):
        # The button and its note say what the name will make: a video or a GIF
        if record.is_gif(self.vars["clip_video"].get().strip()):
            self.make_button.configure(text="Make GIF")
            self.make_note.set("(an animated GIF: Stella's colors, 640 wide, "
                               "50 frames a second, no sound)")
        else:
            self.make_button.configure(text="Make video")
            self.make_note.set("(size, stereo sound and quality: as for recording; "
                               "name it .gif for an animated GIF)")
        self.root.after_idle(self._fit_window)

    def _changed(self, *_):
        self._update()
        if self._save_job:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(500, self.save)

    def _update(self):
        s = self.settings()
        try:
            text = command_line(s, self.found_stella)
        except ValueError:
            text = "(Extra Stella options: a quote isn't closed.)"
        self.command.configure(state="normal")
        self.command.delete("1.0", "end")
        self.command.insert("1.0", text)
        self.command.configure(state="disabled")
        self._fit_command()
        for key in self._folds:
            if s[key] != self._fold_shown.get(key):
                self._show_fold(key, s[key])
        if not self.job:
            self._grey_unsaved(s["save"])

    def _grey_unsaved(self, save):
        """Grey the fields of the file that isn't saved (video or capture)."""
        for widgets, used in ((self.for_video, save != "CAPTURE"),
                              (self.for_capture, save != "VIDEO")):
            for widget in widgets:
                widget.state(["!disabled" if used else "disabled"])

    def _fit_command(self):
        """Make the command box as tall as its wrapped lines (2 to 8)."""
        lines = self.command.count("1.0", "end", "displaylines")
        lines = lines[0] if isinstance(lines, tuple) else lines or 1
        lines = min(max(lines, 2), 8)
        if lines != int(self.command.cget("height")):
            self.command.configure(height=lines)
            self.root.after_idle(self._fit_window)

    def _show_fold(self, key, show):
        button, frame, title = self._folds[key]
        self._fold_shown[key] = show
        button.configure(text=("▾" if show else "▸") + " " + title)
        if show:
            frame.grid()
        else:
            frame.grid_remove()
        self.root.after_idle(self._fit_window)

    def _fit_window(self):
        """Fit the window's height to what it holds, keeping a width the user chose."""
        self.root.update_idletasks()
        if self.root.winfo_ismapped():
            width = max(self.root.winfo_width(), self.root.winfo_reqwidth())
            self.root.geometry(f"{width}x{self.root.winfo_reqheight()}")

    def save(self):
        self._save_job = None
        try:
            save_settings(self.settings_file, self.settings())
        except OSError as e:
            self.status.set(f"Can't save the settings to {self.settings_file}: {e}")

    def close(self):
        """Close the window. A recording is stopped and its video saved first."""
        if self.job:
            self.closing = True
            self.stop()
            return
        self.save()
        for pending in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(pending)  # nothing of this window runs after it
        self.root.destroy()

    # --- recording ---

    def problem(self, clip=False):
        """The first thing to fix before recording (clip: before making a
        video from a capture), as a message; None if nothing."""
        numbers = [("crf", "Quality, under More options")]
        if self.vars["time_limit"].get() and not clip:
            numbers.insert(0, ("seconds", "Stop after"))
        for key, name in numbers:
            low, high = RANGES[key]
            try:
                good = low <= int(self.vars[key].get().strip()) <= high
            except ValueError:
                good = False
            if not good:
                return f"{name}: give a whole number from {low} to {high}."
        s = self.settings()
        return check_clip_fields(s) if clip else check_fields(s, self.found_stella)

    def record(self):
        """Check the fields, then record in a worker thread."""
        if self.job:
            return
        problem = self.problem()
        if problem:
            self._say(problem)
            return
        self.save()
        s = self.settings()
        asked = video_path(s) if s["save"] != "CAPTURE" else None
        asked_capture = capture_path(s) if s["save"] != "VIDEO" else None
        there = there_already(asked, asked_capture)
        video, capture = free_names(asked, asked_capture)
        job = Job(video=video, asked=asked, capture=capture, asked_capture=asked_capture,
                  there=there)
        self._start(job, record.record_rom, s,
                    rom=s["rom"].strip(), out_path=job.video, capture=job.capture,
                    stella=s["stella"].strip() or self.found_stella,
                    stella_args=stella_options(s), sound=s["hear"],
                    own_settings=self.own_settings,
                    seconds=s["seconds"] if s["time_limit"] else None)

    def make_video(self):
        """Check the fields under Video from a capture, then make the video in
        a worker thread."""
        if self.job:
            return
        problem = self.problem(clip=True)
        if problem:
            self._say(problem)
            return
        self.save()
        s = self.settings()
        start, end = clip_times(s)
        asked = clip_video_path(s)
        job = Job(video=free_name(asked), asked=asked, there=there_already(asked),
                  source=Path(s["clip_capture"].strip()))
        self._start(job, record.record_capture, s, path=job.source, out_path=job.video,
                    start=start or None, end=end)

    def _start(self, job, work, s, **kwargs):
        """Run work(**kwargs) for job in a worker thread, with the video
        settings of s; the window locks until it is done."""
        self.job = job
        job.thread = threading.Thread(target=work_in_thread, args=(job.messages, work), kwargs=dict(
            kwargs, ffmpeg=s["ffmpeg"].strip() or "ffmpeg", stop=job.stop,
            finishing=job.finishing, scale=s["scale"], stereo=s["stereo"], crf=s["crf"],
            keep=s["keep"]))
        self._lock(True)
        self.note.set("")
        self._show_job()
        job.thread.start()
        self.root.after(POLL_MS, self._poll)

    def stop(self):
        """Ask the recording to end: Stella is closed and the video finished."""
        if self.job:
            self.job.stop.set()
            self.stop_button.state(["disabled"])
            self._show_job()

    def _lock(self, recording):
        """While recording, the fields can't change and only Stop works."""
        for widget in self.inputs + [self.record_button, self.make_button]:
            widget.state(["disabled" if recording else "!disabled"])
        if not recording:
            self._grey_unsaved(self.settings()["save"])
        self.stop_button.state(["!disabled" if recording else "disabled"])
        if recording:
            self.open_button.state(["disabled"])
            self.folder_button.state(["disabled"])

    def _show_job(self):
        job = self.job
        if self.closing:
            text = f"Saving {job.what}, then closing..."
        elif job.finishing.is_set():
            text = f"Finishing {job.what}..."
        elif job.stop.is_set():
            text = "Stopping..." if job.source else "Stopping: closing Stella..."
        elif job.progress:
            frames, seconds, size = job.progress
            text = (f"{'Making ' + job.what if job.source else 'Recording'}...   "
                    f"{clock(seconds)}   {frames:,} frames   {size / 1e6:.1f} MB")
        else:
            text = "Reading the capture..." if job.source else "Starting Stella..."
        if text != self.status.get():
            self._say(text)

    def _poll(self):
        """Read what the worker said since the last look; every POLL_MS while recording."""
        job = self.job
        while True:
            try:
                kind, what = job.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                job.progress = what
            elif kind == "note":
                job.notes.append(what)
                self.note.set("; ".join(job.notes))
            else:
                self._finished(**{kind: what})
                return
        self._show_job()
        self.root.after(POLL_MS, self._poll)

    def _finished(self, done=None, error=None):
        job, self.job = self.job, None
        job.thread.join()
        self._lock(False)
        if error:
            self.closing = False  # stay open to show why
            self._say(f"{'Making ' + job.what if job.source else 'The recording'} failed. {error}")
            return
        self.last_video = done.path  # the video, or the capture when there is none
        if job.video:
            self.open_button.state(["!disabled"])  # a capture has no player to open it in
        self.folder_button.state(["!disabled"])
        saved = [p for p in (job.video, job.capture) if p]
        asked = [p for p in (job.asked, job.asked_capture) if p]
        try:
            size = ", " + " and ".join(f"{p.stat().st_size / 1e6:.1f} MB" for p in saved)
        except OSError:
            size = ""
        names = f"{saved[0]} and {saved[1].name}" if len(saved) == 2 else str(saved[0])
        renamed = ""
        if saved != asked and job.there:
            there = [p.name for p in job.there]
            listed = ", ".join(there[:-1]) + " and " + there[-1] if len(there) > 1 else there[0]
            renamed = f" ({listed} {'were' if len(there) > 1 else 'was'} there already)"
        self._say(f"Saved {names}{renamed}: {clock(done.seconds)}, "
                  f"{done.frames:,} frames{size}.")
        if self.closing:
            self.close()

    def open_video(self):
        try:
            open_file(self.last_video)
        except OSError as e:
            self._say(f"Can't open {self.last_video}: {e.strerror or e}")

    def show_folder(self):
        try:
            subprocess.Popen(reveal_command(self.last_video))
        except OSError as e:
            self._say(f"Can't show the folder of {self.last_video}: {e.strerror or e}")

    # --- buttons ---

    def _folder_of(self, key):
        """The folder of a path field, for a dialog to start in; None if none."""
        text = self.vars[key].get().strip()
        folder = Path(text).parent if text else None
        return str(folder) if folder and folder.is_dir() else None

    def _browse_rom(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose the game's ROM", initialdir=self._folder_of("rom"),
            filetypes=[("Atari 2600 ROMs", "*.bin *.a26 *.rom"), ("All files", "*")])
        if path:
            self.vars["rom"].set(str(Path(path)))

    def _browse_video(self):
        current = self.vars["video"].get().strip()
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save the video as", initialdir=self._folder_of("video"),
            initialfile=Path(current).name if current else None, defaultextension=".mp4",
            filetypes=[("MP4 video", "*.mp4"), ("All files", "*")])
        if path:
            self.vars["video"].set(str(Path(path)))

    def _browse_capture(self):
        current = self.vars["capture"].get().strip()
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save the capture as", initialdir=self._folder_of("capture"),
            initialfile=Path(current).name if current else None, defaultextension=".sscap",
            filetypes=[("StellaStream capture", "*.sscap"), ("All files", "*")])
        if path:
            self.vars["capture"].set(str(Path(path)))

    def _browse_clip_capture(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="Choose the capture", initialdir=self._folder_of("clip_capture"),
            filetypes=[("StellaStream capture", "*.sscap"), ("All files", "*")])
        if path:
            self.vars["clip_capture"].set(str(Path(path)))

    def _browse_clip_video(self):
        current = self.vars["clip_video"].get().strip()
        types = [("MP4 video", "*.mp4"), ("Animated GIF (no sound)", "*.gif")]
        if record.is_gif(current):
            types.reverse()  # the dialog lists the first type
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Save the video or GIF as",
            initialdir=self._folder_of("clip_video") or self._folder_of("clip_capture"),
            initialfile=Path(current).name if current else None,
            defaultextension=".gif" if record.is_gif(current) else ".mp4",
            filetypes=types + [("All files", "*")])
        if path:
            self.vars["clip_video"].set(str(Path(path)))

    def _browse_program(self, key, title):
        types = [("Programs", "*.exe")] if sys.platform == "win32" else []
        path = filedialog.askopenfilename(parent=self.root, title=title,
                                          initialdir=self._folder_of(key),
                                          filetypes=types + [("All files", "*")])
        if path:
            self.vars[key].set(str(Path(path)))

    def _copy_command(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.command.get("1.0", "end-1c"))
        self.status.set("The Stella command is on the clipboard.")


def _sharp_on_scaled_screens():
    """Windows draws a program that doesn't say it handles screen scaling
    blurred; say so. Other systems need nothing."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def main():
    _sharp_on_scaled_screens()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
