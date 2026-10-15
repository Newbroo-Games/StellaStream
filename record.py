"""Record a video from Stella's raw stream with ffmpeg.

    python record.py capture.raw out.mp4      from a stream file
    python record.py game.sscap out.mp4       from a capture file (sscap.py)
    python record.py --rom game.bin out.mp4   play; close Stella to stop
    (--capture game.sscap with --rom saves a capture of the same play too;
    --from 1:20 --to 1:45 with a capture makes a video of that part)
    python record.py game.sscap out.gif       an animated GIF (gif.py), no sound

Picture: the TIA colors through the stream's palette, scaled with sharp pixels
to the TV shape (4x wide, 2x tall; --scale 2 doubles that), at 60 fps (NTSC)
or 50 fps (PAL). Sound: Stella's own mix (mono, or --stereo) with the same
10 Hz high-pass Stella plays it through, encoded as AAC.

The sound is the clock: each frame is written as many times as the sound says
is due (once, normally), so a game whose frames aren't exactly 262 lines stays
in sync. Video goes to ffmpeg as it arrives, sound to a WAV file; the two are
joined at the end without re-encoding the picture.

A name ending in .gif makes an animated GIF instead (gif.py): Stella's exact
colors, 640 wide, 50 frames a second, no sound, no ffmpeg. From a capture or
a stream file only: a GIF is written slower than the game plays.

As a module: record() turns any stream into a video (record_gif() into a
GIF); record_capture() does it
from a capture file; record_rom() runs Stella on a ROM and records it, and can
be stopped from another thread. They report progress through a callback and
raise RecordError with a message for the user.
"""
import argparse
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import gif
from rawstream import (Audio, End, Frame, Header, Jump, Palette, close_stella,
                       launch_stella, parse)
from sscap import Capture, CaptureError, Writer, is_capture, time_option, time_text

TV_SHAPE = (4, 2)  # a TIA pixel is about twice as wide as a scanline is tall
PROGRESS_EVERY = 0.2  # seconds between progress calls


class RecordError(Exception):
    """A recording failed; the message says what went wrong and what to do."""


@dataclass
class Result:
    path: Path
    frames: int      # video frames written (with no video: the stream's frames)
    seconds: float   # length of the video (with no video: of the capture)
    end_seen: bool   # the stream ended with END: Stella closed normally
    capture: Path = None  # the capture file saved too, if any


def _log(message):
    print(message, file=sys.stderr)


def audio_tables(header, stereo):
    """Byte-translate tables that turn packed TIA volumes into 16-bit PCM bytes."""
    if stereo:
        left = [header.stereo[b & 15] for b in range(256)]
        right = [header.stereo[b >> 4] for b in range(256)]
        chans = (left, right)
    else:
        chans = ([header.mono[(b & 15) + (b >> 4)] for b in range(256)],)
    tables = []
    for values in chans:
        tables.append(bytes(v & 0xff for v in values))
        tables.append(bytes((v >> 8) & 0xff for v in values))
    return tables


def to_pcm(samples, tables):
    """Interleave the translated bytes: lo, hi (mono) or L lo, L hi, R lo, R hi."""
    step = len(tables)
    pcm = bytearray(len(samples) * step)
    for i, table in enumerate(tables):
        pcm[i::step] = samples.translate(table)
    return bytes(pcm)


def check_ffmpeg(ffmpeg="ffmpeg"):
    """Raise RecordError unless ffmpeg runs."""
    try:
        subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        raise RecordError(f"ffmpeg doesn't run ('{ffmpeg}'). Install it from ffmpeg.org "
                          "and put it on the PATH, or choose the program.") from None


def side_files(out_path):
    """The separate picture and sound files record() writes next to the video."""
    out_path = Path(out_path)
    return (out_path.with_name(out_path.stem + ".video.mkv"),
            out_path.with_name(out_path.stem + ".audio.wav"))


def is_gif(path):
    """True when path names a GIF (gif.py) rather than a video."""
    return Path(path).suffix.lower() == ".gif"


def _ffmpeg_failed(what, said):
    """RecordError with what ffmpeg said about it."""
    said = said.strip()
    return RecordError(f"ffmpeg failed while {what}" + (f": {said}" if said else "."))


def record(stream, out_path, scale=1, stereo=False, seconds=None, crf=12, keep=False,
           ffmpeg="ffmpeg", progress=None, stop=None, started=None, finishing=None, log=_log):
    """Turn a raw stream into a video at out_path. Returns a Result.

    progress(frames, seconds, bytes): called about 5 times a second with the
    video so far. stop: a threading.Event; once set, recording ends at the
    next chunk. started: a threading.Event, set when the first HEAD arrives.
    finishing: a threading.Event, set when the stream is over and the video
    is being finished. log(message): short notes for the user. Raises
    RecordError. An out_path ending in .gif makes a GIF (record_gif()), and
    the options for the video and the sound don't apply.
    """
    if is_gif(out_path):
        return record_gif(stream, out_path, seconds=seconds, progress=progress, stop=stop,
                          started=started, finishing=finishing, log=log)
    out_path = Path(out_path)
    video_path, audio_path = side_files(out_path)

    header = None
    pcm_tables = None
    planes = None   # (G, B, R) translate tables; ffmpeg's gbrp order
    height = None   # fixed at the first frame
    encoder = None
    wav = None
    samples = 0
    pcm_bytes = 0
    frames_out = 0
    end_seen = False
    encoder_failed = False
    encoder_said = ""
    blank_row = bytes(160)
    next_progress = 0.0

    def report_progress():
        if not progress or header is None:
            return
        try:
            video_bytes = video_path.stat().st_size
        except OSError:
            video_bytes = 0
        progress(frames_out, frames_out * header.samples_per_frame / header.sample_rate,
                 video_bytes + pcm_bytes)

    try:
        for item in parse(stream):
            if stop is not None and stop.is_set():
                break
            if isinstance(item, Header):
                if header and item.timing != header.timing:
                    log(f"The TV type changed to {item.timing_name}; the video stops there.")
                    break
                if header is None:
                    header = item
                    if started is not None:
                        started.set()
                    pcm_tables = audio_tables(header, stereo)
                    wav = wave.open(str(audio_path), "wb")
                    wav.setnchannels(2 if stereo else 1)
                    wav.setsampwidth(2)
                    wav.setframerate(header.sample_rate)
                    log(f"{header.timing_name}, ROM '{header.rom_name}'")
            elif isinstance(item, Palette):
                rgb = item.rgb
                planes = tuple(bytes(rgb[i * 3 + c] for i in range(256)) for c in (1, 2, 0))
            elif isinstance(item, Audio):
                if wav:
                    pcm = to_pcm(item.samples, pcm_tables)
                    wav.writeframesraw(pcm)
                    samples += len(item.samples)
                    pcm_bytes += len(pcm)
            elif isinstance(item, Frame):
                if header is None or planes is None:
                    continue
                if encoder is None:
                    height = item.height
                    fps = f"{header.sample_rate}/{header.samples_per_frame}"
                    sx, sy = TV_SHAPE[0] * scale, TV_SHAPE[1] * scale
                    try:
                        encoder = subprocess.Popen(
                            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                             "-f", "rawvideo", "-pix_fmt", "gbrp", "-s", f"160x{height}",
                             "-r", fps, "-i", "-",
                             "-vf", f"scale=iw*{sx}:ih*{sy}:flags=neighbor",
                             "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                             "-pix_fmt", "yuv420p", str(video_path)],
                            stdin=subprocess.PIPE, stderr=subprocess.PIPE)
                    except OSError as e:
                        raise RecordError(f"ffmpeg doesn't run ('{ffmpeg}'): {e}") from None
                    log(f"Picture 160x{height} -> {160 * sx}x{height * sy}, "
                        f"{header.sample_rate / header.samples_per_frame:g} fps")
                px = item.pixels[:160 * height]
                if item.height < height:
                    px += blank_row * (height - item.height)
                frame = b"".join(px.translate(t) for t in planes)
                due = samples // header.samples_per_frame - frames_out
                try:
                    for _ in range(due):
                        encoder.stdin.write(frame)
                except OSError:
                    encoder_failed = True
                    break
                frames_out += max(due, 0)
                if time.monotonic() >= next_progress:
                    report_progress()
                    next_progress = time.monotonic() + PROGRESS_EVERY
                if seconds and frames_out >= seconds * header.sample_rate / header.samples_per_frame:
                    break
            elif isinstance(item, End):
                end_seen = True
            elif isinstance(item, Jump):
                pass  # a jump is just a cut in the video
    except KeyboardInterrupt:
        log("Stopped")
    finally:
        if finishing is not None:
            finishing.set()
        if wav:
            wav.close()
        if encoder:
            try:
                encoder.stdin.close()
            except OSError:
                pass
            encoder_said = encoder.stderr.read().decode("utf-8", "replace")
            encoder.stderr.close()
            encoder.wait()

    if encoder and (encoder_failed or encoder.returncode != 0):
        raise _ffmpeg_failed("encoding the picture", encoder_said)
    if not frames_out:
        raise RecordError("No frames were recorded.")
    report_progress()

    mux = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(video_path), "-i", str(audio_path),
         "-map", "0:v", "-map", "1:a", "-c:v", "copy",
         "-af", "highpass=f=10:poles=1", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-shortest", str(out_path)],
        capture_output=True)
    if mux.returncode != 0:
        said = mux.stderr.decode("utf-8", "replace").strip()
        raise RecordError(f"ffmpeg failed while joining picture and sound: {said} "
                          f"(the separate files are kept: {video_path.name}, {audio_path.name})")
    if not keep:
        video_path.unlink()
        audio_path.unlink()
    return Result(out_path, frames_out,
                  frames_out * header.samples_per_frame / header.sample_rate, end_seen)


def record_gif(stream, out_path, seconds=None, progress=None, stop=None, started=None,
               finishing=None, log=_log):
    """Turn a raw stream into an animated GIF at out_path (gif.py), with no
    sound and no ffmpeg. Returns a Result (frames: the GIF's, gif.FPS a
    second). The sound is the clock, as in record(): each GIF frame shows the
    frame on the TV at its time. seconds, progress (bytes: the GIF's so far),
    stop, started, finishing and log work as in record(). Raises RecordError.
    """
    out_path = Path(out_path)
    header = None
    palette = None
    height = None   # fixed at the first frame
    writer = None
    samples = 0
    shown = 0       # GIF frames added
    end_seen = False
    blank_row = bytes(160)
    next_progress = 0.0

    def report_progress():
        if progress and writer:
            progress(shown, shown / gif.FPS, writer.bytes_out)

    try:
        f = open(out_path, "wb")
    except OSError as e:
        raise RecordError(f"Can't write {out_path}: {e.strerror or e}") from None
    try:
        for item in parse(stream):
            if stop is not None and stop.is_set():
                break
            if isinstance(item, Header):
                if header and item.timing != header.timing:
                    log(f"The TV type changed to {item.timing_name}; the GIF stops there.")
                    break
                if header is None:
                    header = item
                    if started is not None:
                        started.set()
                    log(f"{header.timing_name}, ROM '{header.rom_name}'")
            elif isinstance(item, Palette):
                palette = bytes(item.rgb[:768]).ljust(768, bytes(1))
            elif isinstance(item, Audio):
                samples += len(item.samples)
            elif isinstance(item, Frame):
                if header is None or palette is None:
                    continue
                rate, per_frame = header.sample_rate, header.samples_per_frame
                if writer is None:
                    height = item.height
                    writer = gif.Writer(f, height)
                    fps = rate / per_frame
                    sx, sy = gif.SCALE
                    log(f"Picture 160x{height} -> GIF {160 * sx}x{height * sy}, "
                        + (f"{fps:g} fps shown at {gif.FPS}" if fps > gif.FPS
                           else f"{fps:g} fps") + ", no sound")
                px = item.pixels[:160 * height]
                if item.height < height:
                    px += blank_row * (height - item.height)
                # The video's frames so far, as record() counts them; each GIF
                # frame shows the one on the TV when it starts
                due = samples // per_frame
                full = False
                while shown * rate // (gif.FPS * per_frame) < due:
                    writer.add(px, palette)
                    shown += 1
                    if seconds and shown >= seconds * gif.FPS:
                        full = True
                        break
                if time.monotonic() >= next_progress:
                    report_progress()
                    next_progress = time.monotonic() + PROGRESS_EVERY
                if full:
                    break
            elif isinstance(item, End):
                end_seen = True
    except KeyboardInterrupt:
        log("Stopped")
    finally:
        if finishing is not None:
            finishing.set()
        try:
            if writer:
                writer.close()
        finally:
            f.close()
    if not shown:
        out_path.unlink(missing_ok=True)
        raise RecordError("No frames were recorded.")
    report_progress()
    return Result(out_path, shown, shown / gif.FPS, end_seen)


def record_capture(path, out_path, ffmpeg="ffmpeg", log=_log, start=None, end=None, **video):
    """Turn a capture file (.sscap) into a video at out_path. Returns a Result.

    start, end: a clip, the frames from start up to end (seconds of the
    capture; None: its start, its end), as Capture.clip() cuts it. A damaged
    block is skipped, so about a second is missing from the video there, and
    log says so. video: record()'s other options. Raises RecordError.
    """
    path = Path(path)
    if not is_gif(out_path):
        check_ffmpeg(ffmpeg)
    try:
        f = open(path, "rb")
    except OSError as e:
        raise RecordError(f"Can't open the capture: {e}") from None
    with f:
        try:
            capture = Capture(f)
        except CaptureError as e:
            raise RecordError(f"Can't read the capture {path.name}: {e}.") from None
        if not capture.indexed:
            log(f"The capture has no index ({capture.index_problem}); "
                "it is read block by block.")

        def damaged(block, error):
            log(f"Skipped a damaged part of the capture ({error}): about a second "
                "is missing from the video there.")

        if start is None and end is None:
            stream = capture.stream(on_damage=damaged)
        else:
            try:
                stream = capture.clip(start or 0.0, end, on_damage=damaged)
            except ValueError as e:
                raise RecordError(f"Can't cut that clip: {e}.") from None
            length = capture.seconds()
            log(f"Clip from {time_text(start or 0.0)} to "
                f"{time_text(length if end is None else min(end, length))}")
        return record(stream, out_path, ffmpeg=ffmpeg, log=log, **video)


class _Tee:
    """A stream that also hands every byte read from it to a capture Writer."""

    def __init__(self, stream, writer):
        self._stream, self._writer = stream, writer

    def read(self, n=-1):
        data = self._stream.read(n)
        if data:
            self._writer.write(data)
        return data


def save_capture(stream, writer, path, seconds=None, progress=None, stop=None, started=None,
                 finishing=None, log=_log):
    """Save a raw stream as a capture, with no video: writer is the
    sscap.Writer writing path. Returns a Result (frames: the stream's frames).
    seconds, progress (bytes: the capture's), stop, started, finishing and log
    work as in record(). Raises RecordError."""
    header = None
    frames = 0
    end_seen = False
    next_progress = 0.0

    def emulated():
        return writer.cycles / header.cycles_per_second

    def report_progress():
        if progress and header:
            progress(frames, emulated(), writer.bytes_out)

    try:
        for item in parse(_Tee(stream, writer)):
            if stop is not None and stop.is_set():
                break
            if isinstance(item, Header):
                if header is None:
                    header = item
                    if started is not None:
                        started.set()
                    log(f"{header.timing_name}, ROM '{header.rom_name}'")
            elif isinstance(item, Frame) and header:
                frames += 1
                if time.monotonic() >= next_progress:
                    report_progress()
                    next_progress = time.monotonic() + PROGRESS_EVERY
                if seconds and emulated() >= seconds:
                    break
            elif isinstance(item, End):
                end_seen = True
    except KeyboardInterrupt:
        log("Stopped")
    finally:
        if finishing is not None:
            finishing.set()
    if not frames:
        raise RecordError("No frames were recorded.")
    report_progress()
    return Result(Path(path), frames, emulated(), end_seen, capture=Path(path))


def record_rom(rom, out_path=None, *, capture=None, stella=None, stella_args=(), sound=True,
               own_settings=None, head_timeout=10.0, stop=None, progress=None, log=_log,
               ffmpeg="ffmpeg", **video):
    """Run Stella on rom and record it: a video to out_path, a capture file
    (.sscap) to capture, or both from the same stream. Returns a Result (path:
    the video, or the capture when there is no video).

    The recording ends when Stella is closed, when stop (a threading.Event) is
    set, which asks Stella to close, or after video['seconds']. sound: Stella
    plays the game's sound (the files get it either way). own_settings: Stella
    uses a settings folder of its own (default: when silent, as for tests).
    video: record()'s scale, stereo, seconds, crf, keep and finishing. A
    capture alone needs no ffmpeg. Raises RecordError.
    """
    rom = Path(rom)
    if out_path is None and capture is None:
        raise ValueError("nothing to save: give out_path, capture or both")
    out_path = Path(out_path) if out_path else None
    capture = Path(capture) if capture else None
    if out_path and is_gif(out_path):
        raise RecordError("A GIF is made from a capture: save a capture while you play, "
                          "then make the GIF from it.")
    if not rom.is_file():
        raise RecordError(f"The ROM file isn't there: {rom}")
    for target in (out_path, capture):
        if target:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise RecordError(f"Can't make the folder {target.parent}: {e}") from None
    if out_path:
        check_ffmpeg(ffmpeg)
    if own_settings is None:
        own_settings = not sound
    try:
        proc = launch_stella(rom, extra_args=list(stella_args), silent=not sound,
                             own_settings=own_settings, stella=stella)
    except FileNotFoundError as e:
        raise RecordError(f"Stella isn't there: {stella}" if stella
                          else f"Stella doesn't run: {e}") from None
    except OSError as e:
        raise RecordError(f"Stella doesn't run ({stella}): {e}") from None

    stop = stop or threading.Event()
    started = threading.Event()
    finished = threading.Event()
    timed_out = []

    def watch():
        # Close Stella when asked to stop, or if it never starts streaming
        deadline = time.monotonic() + head_timeout
        while not finished.wait(0.1):
            if stop.is_set():
                close_stella(proc)
                return
            if not started.is_set() and time.monotonic() > deadline:
                timed_out.append(True)
                close_stella(proc)
                return

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    capture_file = writer = None
    try:
        if capture:
            try:
                capture_file = open(capture, "wb")
            except OSError as e:
                raise RecordError(f"Can't write the capture {capture}: {e.strerror or e}") from None
            writer = Writer(capture_file, background=True)  # never holds the pipe up
        if out_path:
            result = record(_Tee(proc.stdout, writer) if writer else proc.stdout, out_path,
                            ffmpeg=ffmpeg, progress=progress, started=started, log=log, **video)
        else:
            result = save_capture(proc.stdout, writer, capture, seconds=video.get("seconds"),
                                  progress=progress, started=started,
                                  finishing=video.get("finishing"), log=log)
    except RecordError as e:
        if not started.is_set():
            if timed_out:
                raise RecordError(f"Stella sent no stream in {head_timeout:g} s. Is it a "
                                  "Stella with -rawstream, and does the ROM load?") from None
            raise RecordError("Stella closed without sending a stream. Is it a Stella with "
                              "-rawstream, and does the ROM load?") from None
        if writer and writer.frames:
            raise RecordError(f"{e} The capture was saved: {capture.name}") from None
        raise
    finally:
        finished.set()
        watcher.join()
        proc.stdout.close()  # what's left unread doesn't hold Stella up
        close_stella(proc)
        if writer:
            writer.close()
        if capture_file:
            capture_file.close()
            if writer and not writer.frames:
                capture.unlink(missing_ok=True)  # Stella sent nothing to keep
    if capture:
        result.capture = capture
    return result


def _cli_progress():
    """A progress callback that keeps one status line up to date on a terminal."""
    if not sys.stderr.isatty():
        return None

    def show(frames, seconds, size):
        minutes, secs = divmod(int(seconds), 60)
        print(f"\r{minutes:02d}:{secs:02d}  {frames:,} frames  {size / 1e6:.1f} MB ",
              end="", file=sys.stderr, flush=True)
    return show


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("source", nargs="?",
                    help="stream file written with -rawstream <file>, or a capture (.sscap)")
    ap.add_argument("output", help="video file to write, e.g. out.mp4 (or out.gif)")
    ap.add_argument("--rom", help="run Stella on this ROM and record while you play")
    ap.add_argument("--scale", type=int, default=1, help="multiply the 4x2 TV shape (default 1)")
    ap.add_argument("--stereo", action="store_true", help="channel 0 left, channel 1 right")
    ap.add_argument("--seconds", type=float, help="stop after this much emulated time")
    ap.add_argument("--crf", type=int, default=12, help="x264 quality, lower is better (12)")
    ap.add_argument("--keep", action="store_true", help="keep the separate video and WAV files")
    ap.add_argument("--silent", action="store_true",
                    help="with --rom: no sound from Stella, and its own settings folder")
    ap.add_argument("--stella", help="the Stella to run (default: found automatically)")
    ap.add_argument("--stella-args", default="",
                    help='with --rom: more Stella options, e.g. --stella-args="-lc Driving"')
    ap.add_argument("--ffmpeg", default="ffmpeg", help="the ffmpeg to run (default: ffmpeg)")
    ap.add_argument("--capture", help="with --rom: also save the stream as a capture (.sscap)")
    ap.add_argument("--from", dest="start", type=time_option, metavar="TIME",
                    help="from a capture: the video starts at this time of it (seconds, or "
                         "minutes:seconds)")
    ap.add_argument("--to", dest="end", type=time_option, metavar="TIME",
                    help="from a capture: the video ends at this time of it (default: the end)")
    args = ap.parse_args()

    if bool(args.source) == bool(args.rom):
        ap.error("give a stream file or --rom, not both")
    if args.capture and not args.rom:
        ap.error("--capture goes with --rom")
    clip = args.start is not None or args.end is not None
    if clip and (args.rom or Path(args.source).is_file() and not is_capture(args.source)):
        ap.error("--from and --to cut a clip from a capture (.sscap)")
    if is_gif(args.output):
        if args.rom:
            ap.error("a GIF is made from a capture: record one with --capture game.sscap, "
                     "then: python record.py game.sscap out.gif --from 1:20 --to 1:25")
        if args.scale != 1:
            ap.error("a GIF is always the TV shape at --scale 1 (640 wide)")

    video = dict(scale=args.scale, stereo=args.stereo, seconds=args.seconds,
                 crf=args.crf, keep=args.keep)
    progress = _cli_progress()
    try:
        if args.source and is_capture(args.source):
            result = record_capture(args.source, args.output, ffmpeg=args.ffmpeg,
                                    progress=progress, start=args.start, end=args.end, **video)
        elif args.source:
            if not is_gif(args.output):
                check_ffmpeg(args.ffmpeg)
            with open(args.source, "rb") as f:
                result = record(f, args.output, ffmpeg=args.ffmpeg, progress=progress, **video)
        else:
            # The user is playing: their own settings and sound, unless --silent
            result = record_rom(args.rom, args.output, capture=args.capture, stella=args.stella,
                                stella_args=args.stella_args.split(), sound=not args.silent,
                                ffmpeg=args.ffmpeg, progress=progress, **video)
    except (RecordError, OSError) as e:
        if progress:
            print(file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    if progress:
        print(file=sys.stderr)
    print(f"Wrote {result.path} ({result.frames} frames, {result.seconds:.1f} s)",
          file=sys.stderr)
    if result.capture:
        print(f"Wrote {result.capture}", file=sys.stderr)


if __name__ == "__main__":
    main()
