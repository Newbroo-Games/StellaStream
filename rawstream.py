"""Read Stella's raw stream (-rawstream). The format is in Stella's
docs/rawstream.md.

As a module: parse(stream) yields Header, Palette, Audio, Ram, Frame, Jump and
End objects in stream order; launch_stella() starts Stella writing the stream
to its stdout.

As a program it reports what a stream contains and checks it:

    python rawstream.py capture.raw
    python rawstream.py --rom game.bin --frames 300
    python rawstream.py --rom game.bin --frames 300 --watch Frame,$b6 --sym game.sym

With --rom it runs Stella silently on its own settings folder, reads the
stream live and stops that Stella afterwards. The checks: each frame's audio
holds two samples per scanline, and its cycle count is 76 per scanline past
the previous frame's (both restart after a JUMP or HEAD). --watch prints RAM
bytes for every frame, by address or by name from a DASM .sym file.
"""
import argparse
import collections
import os
import shutil
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT_BASEDIR = HERE / "build" / "stella_base"


def find_stella():
    """The Stella to run when none is given: $STELLASTREAM_STELLA, then a
    Stella source checkout built next to this folder (../stella), then Stella
    on the PATH. None if there is none."""
    if os.environ.get("STELLASTREAM_STELLA"):
        return os.environ["STELLASTREAM_STELLA"]
    checkout = HERE.parent / "stella"
    for build in (checkout / "src" / "os" / "windows" / "x64" / "Release" / "Stella.exe",
                  checkout / "stella"):
        if build.is_file():
            return str(build)
    return shutil.which("Stella") or shutil.which("stella")

TIMINGS = ("NTSC", "PAL", "SECAM")
JUMP_REASONS = ("reset", "state load", "ROM load")


@dataclass
class Header:
    version: int
    timing: int
    sample_rate: int
    cycles_per_second: int
    width: int
    mono: tuple
    stereo: tuple
    rom_name: str
    rom_md5: str

    @property
    def timing_name(self):
        return TIMINGS[self.timing] if self.timing < len(TIMINGS) else f"?{self.timing}"

    @property
    def nominal_lines(self):
        return 262 if self.timing == 0 else 312

    @property
    def samples_per_frame(self):
        """Samples in one nominal frame: the video frame rate is
        sample_rate / samples_per_frame (60 for NTSC, 50 for PAL)."""
        return 2 * self.nominal_lines


@dataclass
class Palette:
    rgb: bytes  # 256 x (R, G, B)


@dataclass
class Audio:
    samples: bytes  # channel 0 volume in bits 0-3, channel 1 in bits 4-7


@dataclass
class Ram:
    data: bytes  # console RAM $80-$FF as the next frame ended

    def __getitem__(self, address):
        return self.data[address - 0x80]


@dataclass
class Frame:
    cycles: int
    number: int
    scanlines: int
    start_line: int
    width: int
    height: int
    pixels: bytes  # width x height TIA color values


@dataclass
class Jump:
    reason: int

    @property
    def name(self):
        r = self.reason
        return JUMP_REASONS[r] if r < len(JUMP_REASONS) else f"?{r}"


class End:
    pass


def _read_exactly(stream, n):
    """A pipe can return less than asked for; keep reading until n or EOF."""
    parts = []
    while n > 0:
        part = stream.read(n)
        if not part:
            break
        parts.append(part)
        n -= len(part)
    return b"".join(parts)


def chunks(stream):
    """Yield (tag, payload) until the end of the stream."""
    while True:
        head = _read_exactly(stream, 8)
        if len(head) < 8:
            return
        tag = head[:4].decode("ascii", "replace")
        (length,) = struct.unpack("<I", head[4:])
        payload = _read_exactly(stream, length)
        if len(payload) < length:
            return  # cut off mid-chunk
        yield tag, payload


def _string(p, off):
    (n,) = struct.unpack_from("<H", p, off)
    return p[off + 2:off + 2 + n].decode("utf-8", "replace"), off + 2 + n


def parse(stream):
    """Yield the stream's chunks as objects; unknown chunks are skipped."""
    for tag, p in chunks(stream):
        if tag == "HEAD":
            version, timing, _, rate, cps, width, _ = struct.unpack_from("<HBBIIHH", p, 0)
            mono = struct.unpack_from("<31h", p, 16)
            stereo = struct.unpack_from("<16h", p, 78)
            name, off = _string(p, 110)
            md5, off = _string(p, off)
            yield Header(version, timing, rate, cps, width, mono, stereo, name, md5)
        elif tag == "PALT":
            yield Palette(p[:768])
        elif tag == "AUDI":
            yield Audio(p)
        elif tag == "RAM ":
            yield Ram(p[:128])
        elif tag == "FRAM":
            cycles, number, lines, start, width, height = struct.unpack_from("<QIHHHH", p, 0)
            yield Frame(cycles, number, lines, start, width, height, p[20:20 + width * height])
        elif tag == "JUMP":
            yield Jump(p[0] if p else 255)
        elif tag == "END ":
            yield End()


def stella_command(rom, extra_args=(), silent=True, own_settings=True, stella=None):
    """The command line launch_stella() runs, as a list.

    silent: no sound (every automated run). own_settings: a settings folder of
    its own under build/, so the user's Stella settings are never touched.
    stella: the program to run; find_stella() when not given.
    """
    stella = stella or find_stella()
    if not stella:
        raise FileNotFoundError("no Stella found; choose one, or set STELLASTREAM_STELLA")
    args = [str(stella), "-rawstream", "-"]
    if silent:
        args += ["-audio.enabled", "0"]
    if own_settings:
        args += ["-basedir", str(SCRIPT_BASEDIR)]
    return args + list(extra_args) + [str(rom)]


def launch_stella(rom, extra_args=(), silent=True, own_settings=True, stella=None):
    """Start Stella writing the raw stream to its stdout; read proc.stdout.
    The arguments are stella_command()'s."""
    args = stella_command(rom, extra_args, silent, own_settings, stella)
    if own_settings:
        SCRIPT_BASEDIR.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            bufsize=0)


def close_stella(proc, timeout=5.0):
    """Ask a Stella started by launch_stella() to close the way a user would,
    so it ends its stream with END; if it hasn't closed after timeout
    seconds, end it. Only that one process is touched."""
    if proc.poll() is not None:
        return
    if not (sys.platform == "win32" and _post_close(proc.pid)):
        proc.terminate()  # elsewhere SIGTERM, which SDL turns into a normal quit
    try:
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _post_close(pid):
    """Windows: post WM_CLOSE to the visible windows of process pid, like
    clicking their close button. True if it had any."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    windows = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            windows.append(hwnd)
        return True

    user32.EnumWindows(each, 0)
    for hwnd in windows:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    return bool(windows)


def load_symbols(path):
    """A DASM .sym file ('name  address  (flags)' lines) as {name: address}."""
    symbols = {}
    with open(path, encoding="latin-1") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    symbols[parts[0]] = int(parts[1], 16)
                except ValueError:
                    pass
    return symbols


def watch_list(spec, symbols=None):
    """'Frame,$b6,0xb7' -> [(label, address)], each in console RAM."""
    watches = []
    for item in (s.strip() for s in spec.split(",") if s.strip()):
        if symbols and item in symbols:
            address = symbols[item]
        else:
            try:
                address = int(item.lstrip("$"), 16)
            except ValueError:
                raise ValueError(f"'{item}' is not an address or a symbol") from None
        if 0x180 <= address <= 0x1ff:
            address -= 0x100  # the stack mirror of the same RAM
        if not 0x80 <= address <= 0xff:
            raise ValueError(f"{item} = ${address:x} is not console RAM ($80-$FF)")
        watches.append((item, address))
    return watches


def report(stream, max_frames=None, verbose=False, watches=(), out=sys.stdout):
    """Print what the stream holds and check that picture and sound line up.
    With watches, print those RAM bytes for every frame.
    Returns True when every check passed."""
    frames = 0
    ram_frames = 0
    ram = None
    samples_total = 0
    lines_seen = collections.Counter()
    heights_seen = collections.Counter()
    odd_frames = []
    audio_ok = audio_bad = cycles_ok = cycles_bad = 0
    worst_audio = worst_cycles = 0
    pending = 0
    prev = None  # previous frame of an unbroken run
    header = None

    def p(*a):
        print(*a, file=out)

    for item in parse(stream):
        if isinstance(item, Header):
            header = item
            prev = None
            p(f"HEAD  v{item.version} {item.timing_name}, {item.sample_rate} Hz, "
              f"{item.cycles_per_second} cycles/s, width {item.width}, "
              f"ROM '{item.rom_name}' {item.rom_md5}")
        elif isinstance(item, Palette):
            p("PALT  " + " ".join(item.rgb[i * 3:i * 3 + 3].hex() for i in range(0, 256, 32))
              + "  (every 32nd color)")
        elif isinstance(item, Audio):
            pending += len(item.samples)
            samples_total += len(item.samples)
        elif isinstance(item, Ram):
            ram = item
        elif isinstance(item, Jump):
            p(f"JUMP  {item.name} after frame {frames}")
            prev = None
        elif isinstance(item, End):
            p("END")
        elif isinstance(item, Frame):
            frames += 1
            lines_seen[item.scanlines] += 1
            heights_seen[(item.start_line, item.height)] += 1
            if header and item.scanlines != header.nominal_lines and len(odd_frames) < 20:
                odd_frames.append((item.number, item.scanlines))
            if prev is not None:
                d_audio = pending - 2 * item.scanlines
                d_cycles = (item.cycles - prev.cycles) - 76 * item.scanlines
                worst_audio = max(worst_audio, abs(d_audio))
                worst_cycles = max(worst_cycles, abs(d_cycles))
                if d_audio == 0:
                    audio_ok += 1
                else:
                    audio_bad += 1
                if d_cycles == 0:
                    cycles_ok += 1
                else:
                    cycles_bad += 1
            if ram is not None:
                ram_frames += 1
            if verbose or watches:
                line = f"FRAM  #{item.number}"
                if verbose:
                    line += (f" {item.scanlines} lines, rows {item.start_line}+{item.height},"
                             f" {pending} samples, cycle {item.cycles}")
                if watches and ram is not None:
                    line += "  " + "  ".join(f"{name}=${ram[address]:02x}"
                                             for name, address in watches)
                p(line)
            pending = 0
            ram = None  # each frame's RAM comes just before it
            prev = item
            if max_frames and frames >= max_frames:
                break

    p("")
    p(f"Frames: {frames}")
    if header and frames:
        secs = samples_total / header.sample_rate
        p(f"Sound:  {samples_total} samples = {secs:.2f} s of emulated time")
    p(f"RAM:    with {ram_frames} of {frames} frames")
    p("Scanlines per frame: " + ", ".join(f"{k} x{v}" for k, v in sorted(lines_seen.items())))
    p("Rows shown (first scanline + height): "
      + ", ".join(f"{s}+{h} x{v}" for (s, h), v in sorted(heights_seen.items())))
    if odd_frames:
        p("Frames that aren't " + str(header.nominal_lines) + " lines (first 20): "
          + ", ".join(f"#{n}={l}" for n, l in odd_frames))
    p(f"Check, 2 samples per scanline: {audio_ok} ok, {audio_bad} off "
      f"(worst by {worst_audio} samples)")
    p(f"Check, 76 cycles per scanline: {cycles_ok} ok, {cycles_bad} off "
      f"(worst by {worst_cycles} cycles)")
    return frames > 0 and audio_bad == 0 and cycles_bad == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("capture", nargs="?", help="a stream file written with -rawstream <file>")
    ap.add_argument("--rom", help="run Stella on this ROM and read its stream live")
    ap.add_argument("--frames", type=int, default=0, help="stop after this many frames")
    ap.add_argument("--verbose", "-v", action="store_true", help="one line per frame")
    ap.add_argument("--watch", default="",
                    help="RAM to print every frame: addresses or symbols, e.g. Frame,$b6")
    ap.add_argument("--sym", help="DASM .sym file for --watch names")
    ap.add_argument("--stella", help="the Stella to run (default: found automatically)")
    ap.add_argument("--stella-args", default="",
                    help='with --rom: more Stella options, e.g. --stella-args="-lc Driving"')
    args = ap.parse_args()

    if bool(args.capture) == bool(args.rom):
        ap.error("give a capture file or --rom, not both")
    try:
        watches = watch_list(args.watch, load_symbols(args.sym) if args.sym else None)
    except (OSError, ValueError) as e:
        ap.error(str(e))

    if args.capture:
        with open(args.capture, "rb") as f:
            ok = report(f, args.frames, args.verbose, watches)
    else:
        if not args.frames:
            ap.error("--rom needs --frames so the run ends")
        proc = launch_stella(args.rom, extra_args=args.stella_args.split(), stella=args.stella)
        try:
            ok = report(proc.stdout, args.frames, args.verbose, watches)
        finally:
            proc.stdout.close()  # what's left unread doesn't hold Stella up
            close_stella(proc)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
