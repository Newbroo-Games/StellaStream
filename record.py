"""Record a video from Stella's raw stream with ffmpeg.

    python record.py capture.raw out.mp4      from a stream file
    python record.py --rom game.bin out.mp4   play; close Stella to stop

Picture: the TIA colors through the stream's palette, scaled with sharp pixels
to the TV shape (4x wide, 2x tall; --scale 2 doubles that), at 60 fps (NTSC)
or 50 fps (PAL). Sound: Stella's own mix (mono, or --stereo) with the same
10 Hz high-pass Stella plays it through, encoded as AAC.

The sound is the clock: each frame is written as many times as the sound says
is due (once, normally), so a game whose frames aren't exactly 262 lines stays
in sync. Video goes to ffmpeg as it arrives, sound to a WAV file; the two are
joined at the end without re-encoding the picture.

As a module: record() turns any stream into a video; record_rom() runs Stella
on a ROM and records it, and can be stopped from another thread. Both report
progress through a callback and raise RecordError with a message for the user.
"""
import argparse
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path

from rawstream import (Audio, End, Frame, Header, Jump, Palette, close_stella,
                       launch_stella, parse)

TV_SHAPE = (4, 2)  # a TIA pixel is about twice as wide as a scanline is tall
PROGRESS_EVERY = 0.2  # seconds between progress calls


class RecordError(Exception):
    """A recording failed; the message says what went wrong and what to do."""


@dataclass
class Result:
    path: Path
    frames: int      # video frames written
    seconds: float   # length of the video
    end_seen: bool   # the stream ended with END: Stella closed normally


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
    RecordError.
    """
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


def record_rom(rom, out_path, *, stella=None, stella_args=(), sound=True, own_settings=None,
               head_timeout=10.0, stop=None, progress=None, log=_log, ffmpeg="ffmpeg", **video):
    """Run Stella on rom and record it to out_path. Returns a Result.

    The recording ends when Stella is closed, when stop (a threading.Event) is
    set, which asks Stella to close, or after video['seconds']. sound: Stella
    plays the game's sound (the video gets it either way). own_settings: Stella
    uses a settings folder of its own (default: when silent, as for tests).
    video: record()'s scale, stereo, seconds, crf, keep and finishing. Raises
    RecordError.
    """
    rom = Path(rom)
    out_path = Path(out_path)
    if not rom.is_file():
        raise RecordError(f"The ROM file isn't there: {rom}")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise RecordError(f"Can't make the video's folder {out_path.parent}: {e}") from None
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
    try:
        return record(proc.stdout, out_path, ffmpeg=ffmpeg, progress=progress,
                      started=started, log=log, **video)
    except RecordError:
        if started.is_set():
            raise
        if timed_out:
            raise RecordError(f"Stella sent no stream in {head_timeout:g} s. Is it a Stella "
                              "with -rawstream, and does the ROM load?") from None
        raise RecordError("Stella closed without sending a stream. Is it a Stella with "
                          "-rawstream, and does the ROM load?") from None
    finally:
        finished.set()
        watcher.join()
        proc.stdout.close()  # what's left unread doesn't hold Stella up
        close_stella(proc)


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
    ap.add_argument("source", nargs="?", help="stream file written with -rawstream <file>")
    ap.add_argument("output", help="video file to write, e.g. out.mp4")
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
    args = ap.parse_args()

    if bool(args.source) == bool(args.rom):
        ap.error("give a stream file or --rom, not both")

    video = dict(scale=args.scale, stereo=args.stereo, seconds=args.seconds,
                 crf=args.crf, keep=args.keep)
    progress = _cli_progress()
    try:
        if args.source:
            check_ffmpeg(args.ffmpeg)
            with open(args.source, "rb") as f:
                result = record(f, args.output, ffmpeg=args.ffmpeg, progress=progress, **video)
        else:
            # The user is playing: their own settings and sound, unless --silent
            result = record_rom(args.rom, args.output, stella=args.stella,
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


if __name__ == "__main__":
    main()
