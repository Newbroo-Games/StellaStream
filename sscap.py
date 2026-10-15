"""StellaStream capture files (.sscap): a raw stream packed in self-contained
blocks with an index. The format is in docs/capture-format.md.

As a module: Writer packs a raw stream as it arrives, in pieces of any size;
Capture reads a file back, a block at a time or as one raw stream.

As a program:

    python sscap.py game.raw game.sscap      pack a raw stream
    python sscap.py game.sscap game.raw      unpack it again
    python sscap.py game.sscap               report what it holds, check every block
    python sscap.py --rom game.bin game.sscap --frames 600
    python sscap.py game.sscap clip.sscap --from 1:20 --to 1:45

With --rom it runs Stella and saves its stream as a capture until Stella is
closed (or, with --frames, asks Stella to close after that many frames).
--from and --to cut a clip from a capture: into a new capture when the file
written ends in .sscap, into a raw stream otherwise.
"""
import argparse
import bisect
import lzma
import math
import os
import queue
import re
import struct
import sys
import threading
import time
import zlib
from dataclasses import dataclass

from rawstream import close_stella, launch_stella, parse

try:
    from compression import zstd  # Python 3.14 and later
except ImportError:
    zstd = None

MAGIC = b"\x89SSC\r\n\x1a\n"
VERSION = 1
FILE_HEADER = struct.Struct("<8sHHBBBB")      # 16 bytes, then the delta-coded tags
BLOCK_HEADER = struct.Struct("<4sIIIBBHIQI")  # 36
INDEX_HEADER = struct.Struct("<4sIIQI")       # 24
INDEX_ENTRY = struct.Struct("<QIQHBB")        # 24
TRAILER = struct.Struct("<QI4s")              # 16

STORED, ZLIB, XZ, ZSTD = 0, 1, 2, 3
METHODS = {"stored": STORED, "zlib": ZLIB, "xz": XZ, "zstd": ZSTD}
METHOD_NAMES = {v: k for k, v in METHODS.items()}
DEFAULT_LEVEL = {ZLIB: 6, XZ: 6, ZSTD: 3}

HAS_HEAD, HAS_PALT, HAS_JUMP, HAS_END, CUT = 1, 2, 4, 8, 16
FLAG_TAGS = {b"HEAD": HAS_HEAD, b"PALT": HAS_PALT, b"JUMP": HAS_JUMP, b"END ": HAS_END}

DEFAULT_DELTA = (b"FRAM", b"RAM ")  # sound compresses better as it is (measured)
NTSC_CYCLES_PER_SECOND = 1194720  # until a HEAD says otherwise
MAX_BLOCK_RAW = 64 << 20          # a stream with no frames still gets blocks
MAX_BLOCK_CYCLES = 0xFFFFFFFF - 76 * 0xFFFF  # room for one more frame
VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".gif"}  # record.py's job


class CaptureError(Exception):
    """A capture file can't be read; the message says where and why."""


def tag_bytes(tag):
    """'RAM' or b'RAM ' -> b'RAM ': four bytes, padded with spaces."""
    t = tag.encode("ascii") if isinstance(tag, str) else bytes(tag)
    if not 1 <= len(t) <= 4:
        raise ValueError(f"a tag is four characters: {tag!r}")
    return t.ljust(4, b" ")


def _xor(a, b):
    """Two byte strings of the same length XORed, as whole integers (fast)."""
    return (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(len(a), "little")


def chunks_in(raw):
    """Yield (tag, chunk) for each whole chunk in raw stream bytes; chunk is
    the tag, length and payload together."""
    pos = 0
    while pos + 8 <= len(raw):
        (n,) = struct.unpack_from("<I", raw, pos + 4)
        if pos + 8 + n > len(raw):
            return
        yield raw[pos:pos + 4], raw[pos:pos + 8 + n]
        pos += 8 + n


def delta_encode(raw, delta_tags):
    """Raw stream bytes -> the block's delta-coded bytes: each chunk of a
    delta-coded tag XORed with the previous raw payload of that tag when the
    lengths match. decode_block() undoes it."""
    out = bytearray()
    last = {}
    pos = 0
    while pos + 8 <= len(raw):
        tag = raw[pos:pos + 4]
        (n,) = struct.unpack_from("<I", raw, pos + 4)
        payload = raw[pos + 8:pos + 8 + n]
        if len(payload) < n:
            break  # cut off inside this chunk
        if tag in delta_tags:
            before = last.get(tag)
            last[tag] = payload
            if before is not None and len(before) == n:
                payload = _xor(payload, before)
        out += raw[pos:pos + 8]
        out += payload
        pos += 8 + n
    out += raw[pos:]
    return bytes(out)


def compress(data, method, level=None):
    """Delta-coded bytes -> a block's stored data."""
    if level is None:
        level = DEFAULT_LEVEL.get(method)
    if method == STORED:
        return data
    if method == ZLIB:
        return zlib.compress(data, level)
    if method == XZ:
        return lzma.compress(data, format=lzma.FORMAT_XZ, check=lzma.CHECK_NONE, preset=level)
    if method == ZSTD:
        return zstd.compress(data, level=level)
    raise ValueError(f"unknown compression method {method}")


# The spec's reference decoder, as it is there (docs/capture-format.md)
def decode_block(data, method, delta_tags):
    """A block's stored data -> the raw stream bytes it gives back."""
    if method == 1:
        data = zlib.decompress(data)
    elif method == 2:
        data = lzma.decompress(data)
    elif method == 3:
        from compression import zstd  # Python 3.14 and later
        data = zstd.decompress(data)
    elif method != 0:
        raise ValueError(f"unknown compression method {method}")
    out = bytearray()
    last = {}  # the last raw payload of each delta-coded tag
    pos = 0
    while pos + 8 <= len(data):
        tag = data[pos:pos + 4]
        (n,) = struct.unpack_from("<I", data, pos + 4)
        payload = data[pos + 8:pos + 8 + n]
        if len(payload) < n:
            break  # the stream was cut off inside this chunk
        if tag in delta_tags:
            before = last.get(tag)
            if before is not None and len(before) == n:
                payload = (int.from_bytes(payload, "little")
                           ^ int.from_bytes(before, "little")).to_bytes(n, "little")
            last[tag] = payload
        out += data[pos:pos + 8] + payload
        pos += 8 + n
    out += data[pos:]  # a cut-off chunk's bytes, as they are
    return bytes(out)


class Writer:
    """Packs a raw stream into a capture file as it arrives.

    write(data) takes the stream in pieces of any size; close() writes the
    last block, the index and the trailer (also on leaving a with block).
    A block closes after the frame that brings it to block_seconds of
    emulated time. recovery: copies of every HEAD and PALT in the index.
    background: blocks are packed and written by a thread of their own, so
    write() never waits for the compressor (live streams: a pipe that isn't
    read holds Stella up). file: a binary file open for writing; it is not
    closed here.
    """

    def __init__(self, file, method=XZ, level=None, delta_tags=DEFAULT_DELTA,
                 block_seconds=1.0, recovery=True, background=False):
        if method not in METHOD_NAMES:
            raise ValueError(f"unknown compression method {method}")
        if method == ZSTD and zstd is None:
            raise ValueError("Zstandard needs Python 3.14 or later")
        if not block_seconds > 0:
            raise ValueError("block_seconds must be more than 0")
        self.method = method
        self.level = level
        self.delta_tags = tuple(dict.fromkeys(tag_bytes(t) for t in delta_tags))
        if len(self.delta_tags) > 255:
            raise ValueError("at most 255 delta-coded tags")
        self.block_seconds = block_seconds
        self.recovery = recovery
        self._file = file
        n = len(self.delta_tags)
        header = (FILE_HEADER.pack(MAGIC, VERSION, FILE_HEADER.size + 4 * n, BLOCK_HEADER.size,
                                   INDEX_ENTRY.size, n, 0) + b"".join(self.delta_tags))
        file.write(header)
        self._offset = len(header)
        self._buf = bytearray()    # bytes that don't make a whole chunk yet
        self._block = bytearray()  # the raw bytes of the block being collected
        self._flags = self._block_frames = self._block_cycles = 0
        self._done_frames = self._done_cycles = 0  # in the blocks written
        self._cps = NTSC_CYCLES_PER_SECOND
        self._entries = []
        self._copies = []          # (block number, HEAD or PALT chunk)
        self.blocks = 0            # blocks closed (packed, or waiting to be)
        self.bytes_in = 0          # raw stream bytes taken
        self.bytes_out = len(header)
        self.closed = False
        self._queue = self._worker = self._error = None
        if background:
            self._queue = queue.Queue(maxsize=4)  # about 9 MB of raw blocks at most
            self._worker = threading.Thread(target=self._work, name="sscap writer", daemon=True)
            self._worker.start()

    @property
    def frames(self):
        """FRAM chunks taken so far."""
        return self._done_frames + self._block_frames

    @property
    def cycles(self):
        """Capture time so far, in emulated CPU cycles."""
        return self._done_cycles + self._block_cycles

    def write(self, data):
        if self.closed:
            raise ValueError("write to a closed capture")
        self._check()
        self.bytes_in += len(data)
        buf = self._buf
        buf += data
        pos = 0
        while len(buf) - pos >= 8:
            (n,) = struct.unpack_from("<I", buf, pos + 4)
            end = pos + 8 + n
            if end > len(buf):
                break
            self._take(bytes(buf[pos:end]))
            pos = end
        del buf[:pos]

    def _take(self, chunk):
        """Add one whole chunk to the block, and close the block when due."""
        tag = chunk[:4]
        self._block += chunk
        self._flags |= FLAG_TAGS.get(tag, 0)
        if tag == b"FRAM":
            lines = struct.unpack_from("<H", chunk, 20)[0] if len(chunk) >= 22 else 0
            self._block_frames += 1
            self._block_cycles += 76 * lines
        elif tag in (b"HEAD", b"PALT"):
            if tag == b"HEAD" and len(chunk) >= 20:
                self._cps = struct.unpack_from("<I", chunk, 16)[0] or NTSC_CYCLES_PER_SECOND
            if self.recovery:
                self._copies.append((self.blocks, chunk))
        if ((tag == b"FRAM" and self._block_cycles >= self.block_seconds * self._cps)
                or self._block_frames == 0xFFFF or self._block_cycles >= MAX_BLOCK_CYCLES
                or len(self._block) >= MAX_BLOCK_RAW):
            self._write_block()

    def _write_block(self):
        """Close the block being collected and pack it, here or in the background."""
        if not self._block:
            return
        block = (bytes(self._block), self._flags, self._block_frames, self._done_frames,
                 self._done_cycles, self._block_cycles)
        self.blocks += 1
        self._done_frames += self._block_frames
        self._done_cycles += self._block_cycles
        self._block = bytearray()
        self._flags = self._block_frames = self._block_cycles = 0
        if self._queue is None:
            self._pack(*block)
        else:
            self._check()
            self._queue.put(block)

    def _pack(self, raw, flags, frames, frame_index, capture_time, cycles):
        data = compress(delta_encode(raw, self.delta_tags), self.method, self.level)
        header = BLOCK_HEADER.pack(b"SSCB", len(data), len(raw), zlib.crc32(raw), self.method,
                                   flags, frames, frame_index, capture_time, cycles)
        self._entries.append(INDEX_ENTRY.pack(self._offset, frame_index, capture_time, frames,
                                              flags, self.method))
        self._file.write(header + data)
        self._file.flush()  # a reader can use every whole block at once
        self._offset += len(header) + len(data)
        self.bytes_out += len(header) + len(data)

    def _work(self):
        """The background thread: pack blocks in order until close() says stop."""
        while (block := self._queue.get()) is not None:
            if self._error is None:
                try:
                    self._pack(*block)
                except Exception as e:  # a full disk, say: raised in the caller's thread
                    self._error = e

    def _check(self):
        if self._error is not None:
            raise self._error

    def close(self):
        """Write the last block (with a stream cut off mid-chunk, its bytes as
        they are), then the index and the trailer."""
        if self.closed:
            return
        self.closed = True
        if self._buf:
            self._block += self._buf
            self._flags |= CUT
            self._buf = bytearray()
        self._write_block()
        if self._worker is not None:
            self._queue.put(None)
            self._worker.join()
            self._check()
        index = (INDEX_HEADER.pack(b"SSCI", self.blocks, self._done_frames, self._done_cycles,
                                   len(self._copies))
                 + b"".join(self._entries)
                 + b"".join(struct.pack("<I", b) + c for b, c in self._copies))
        tail = index + TRAILER.pack(self._offset, zlib.crc32(index), b"SSCE")
        self._file.write(tail)
        self._file.flush()
        self.bytes_out += len(tail)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@dataclass
class Block:
    offset: int        # file offset of the block header
    frame_index: int   # FRAM chunks in the file before this block
    capture_time: int  # emulated CPU cycles in the file before this block
    frames: int        # FRAM chunks in this block
    flags: int
    method: int


class Capture:
    """A capture file opened for reading.

    blocks: a Block for each block, from the index, or found by walking the
    block headers when there is none (the writer was cut off); indexed says
    which, and index_problem why there was no usable index. recovery: the
    index's (block number, chunk) copies of HEAD and PALT. file: a binary
    file open for reading, with seek.
    """

    def __init__(self, file):
        self._file = file
        file.seek(0, 2)
        self.size = file.tell()
        file.seek(0)
        head = file.read(FILE_HEADER.size)
        if len(head) < FILE_HEADER.size or head[:8] != MAGIC:
            raise CaptureError("not a StellaStream capture (.sscap) file")
        _, version, header_len, block_len, entry_len, n, _ = FILE_HEADER.unpack(head)
        if version != VERSION:
            raise CaptureError(f"capture format version {version}; this StellaStream reads "
                               f"version {VERSION}")
        if (header_len < FILE_HEADER.size + 4 * n or block_len < BLOCK_HEADER.size
                or entry_len < INDEX_ENTRY.size):
            raise CaptureError("the file header is damaged")
        tags = file.read(4 * n)
        self.version = version
        self.delta_tags = tuple(tags[i:i + 4] for i in range(0, len(tags), 4))
        self._header_len, self._block_len, self._entry_len = header_len, block_len, entry_len
        self.blocks = []
        self.recovery = []
        self.frames = self.cycles = 0
        self.indexed = False
        self.index_problem = None
        try:
            self._read_index()
            self.indexed = True
        except CaptureError as e:
            self.index_problem = str(e)
            self.blocks, self.recovery = [], []
            self.frames = self.cycles = 0
            self._scan()

    def _read_index(self):
        f = self._file
        if self.size < self._header_len + INDEX_HEADER.size + TRAILER.size:
            raise CaptureError("no index: the writer was cut off")
        f.seek(self.size - TRAILER.size)
        offset, crc, marker = TRAILER.unpack(f.read(TRAILER.size))
        if marker != b"SSCE":
            raise CaptureError("no index: the writer was cut off")
        if not self._header_len <= offset <= self.size - TRAILER.size - INDEX_HEADER.size:
            raise CaptureError("the trailer points outside the file")
        f.seek(offset)
        index = f.read(self.size - TRAILER.size - offset)
        if zlib.crc32(index) != crc:
            raise CaptureError("the index fails its CRC")
        marker, count, self.frames, self.cycles, copies = INDEX_HEADER.unpack_from(index)
        if marker != b"SSCI":
            raise CaptureError("the trailer doesn't point at an index")
        pos = INDEX_HEADER.size
        if pos + count * self._entry_len > len(index):
            raise CaptureError("the index is cut short")
        for _ in range(count):
            self.blocks.append(Block(*INDEX_ENTRY.unpack_from(index, pos)))
            pos += self._entry_len
        for _ in range(copies):
            if pos + 12 > len(index):
                raise CaptureError("the recovery copies are cut short")
            (block,) = struct.unpack_from("<I", index, pos)
            (n,) = struct.unpack_from("<I", index, pos + 8)
            chunk = index[pos + 4:pos + 12 + n]
            if len(chunk) < 8 + n:
                raise CaptureError("the recovery copies are cut short")
            self.recovery.append((block, chunk))
            pos += 12 + n

    def _scan(self):
        """Find the blocks by walking their headers: they end at the first
        that doesn't start with SSCB or doesn't fit in the file."""
        f = self._file
        pos = self._header_len
        while pos + self._block_len <= self.size:
            f.seek(pos)
            (marker, stored, _, _, method, flags, frames, frame_index, capture_time,
             cycles) = BLOCK_HEADER.unpack_from(f.read(self._block_len))
            if marker != b"SSCB" or pos + self._block_len + stored > self.size:
                break
            self.blocks.append(Block(pos, frame_index, capture_time, frames, flags, method))
            self.frames = frame_index + frames
            self.cycles = capture_time + cycles
            pos += self._block_len + stored

    def read_block(self, i):
        """Block i decoded: the raw stream bytes it gives back, checked
        against its CRC. Raises CaptureError if it is damaged or unreadable."""
        b = self.blocks[i]
        f = self._file
        f.seek(b.offset)
        head = f.read(self._block_len)
        if len(head) < self._block_len or head[:4] != b"SSCB":
            raise CaptureError(f"block {i}: no block header at offset {b.offset}")
        _, stored, raw_len, crc, method, *_ = BLOCK_HEADER.unpack_from(head)
        data = f.read(stored)
        if len(data) < stored:
            raise CaptureError(f"block {i} is cut short")
        if method >= 128:
            raise CaptureError(f"block {i} uses private compression method {method}; "
                               "StellaStream can't read it")
        if method == ZSTD and zstd is None:
            raise CaptureError(f"block {i} is compressed with Zstandard; reading it needs "
                               "Python 3.14 or later")
        try:
            raw = decode_block(data, method, self.delta_tags)
        except (ValueError, zlib.error, lzma.LZMAError, EOFError) as e:
            raise CaptureError(f"block {i} can't be decoded: {e}") from None
        except Exception as e:
            if zstd is not None and isinstance(e, zstd.ZstdError):
                raise CaptureError(f"block {i} can't be decoded: {e}") from None
            raise
        if len(raw) != raw_len or zlib.crc32(raw) != crc:
            raise CaptureError(f"block {i} is damaged: it fails its CRC")
        return raw

    def context(self, i):
        """(HEAD, PALT): the chunks in effect where block i starts, from the
        last earlier block that holds each, or from its recovery copies if
        that block is damaged. None for one no earlier block holds."""
        return tuple(self._last_before(i, flag, tag)
                     for flag, tag in ((HAS_HEAD, b"HEAD"), (HAS_PALT, b"PALT")))

    def _last_before(self, i, flag, tag):
        for j in range(i - 1, -1, -1):
            if self.blocks[j].flags & flag:
                try:
                    raw = self.read_block(j)
                except CaptureError:
                    copies = [c for b, c in self.recovery if b == j and c[:4] == tag]
                    if copies:
                        return copies[-1]
                    raise
                found = [c for t, c in chunks_in(raw) if t == tag]
                return found[-1] if found else None
        return None

    def block_at(self, frame=None, cycles=None):
        """The number of the block holding frame number `frame` (counted from 0
        in the file) or the moment `cycles` of capture time."""
        if (frame is None) == (cycles is None):
            raise ValueError("give frame or cycles")
        if frame is not None:
            keys, value, total = [b.frame_index for b in self.blocks], frame, self.frames
        else:
            keys, value, total = [b.capture_time for b in self.blocks], cycles, self.cycles
        if not 0 <= value < total:
            raise IndexError(f"{value} is outside the capture (0 to {total - 1})")
        return bisect.bisect_right(keys, value) - 1

    def stream(self, start=0, on_damage=None):
        """The raw stream as a file-like object (read(n)) for rawstream.parse().
        From block 0 it is the raw stream byte for byte; from a later block it
        starts with the HEAD and PALT in effect there.

        on_damage(block number, CaptureError): if given, a damaged block is
        reported through it and skipped, its recovery copies of HEAD and PALT
        standing in for it; without it, reading one raises CaptureError.
        """
        return _RawStream(self, start, on_damage)

    def first_head(self):
        """The HEAD of the first block that holds one (from the recovery
        copies if that block is damaged), or None."""
        for i, b in enumerate(self.blocks):
            if b.flags & HAS_HEAD:
                return self._last_before(i + 1, HAS_HEAD, b"HEAD")
        return None

    def cycles_per_second(self):
        """Emulated CPU cycles a second, from the first HEAD (NTSC's when it
        can't be read)."""
        try:
            head = self.first_head()
        except CaptureError:
            head = None
        cps = struct.unpack_from("<I", head, 16)[0] if head and len(head) >= 20 else 0
        return cps or NTSC_CYCLES_PER_SECOND

    def seconds(self):
        """The capture's length in seconds of emulated time."""
        return self.cycles / self.cycles_per_second()

    def clip(self, start=0.0, end=None, on_damage=None):
        """The frames that start at or after `start` and before `end` (seconds
        of capture time; end None: to the end) as a raw stream, a file-like
        object for rawstream.parse().

        The clip begins with the HEAD and PALT in effect at its first frame,
        and each frame keeps the sound and RAM sent before it; Stella's own
        frame numbers and cycle counts are unchanged. A clip that runs to the
        end keeps what follows the last frame (END, say), so a clip of
        everything is the raw stream byte for byte. on_damage as in stream().
        Raises ValueError when the times are out of order or start after the
        capture's end.
        """
        cps = self.cycles_per_second()
        first = round(start * cps)
        last = math.inf if end is None else round(end * cps)
        if start < 0:
            raise ValueError("a clip can't start before 0:00")
        if end is not None and end <= start:
            raise ValueError(f"the clip ends ({time_text(end)}) before it starts "
                             f"({time_text(start)})")
        if first >= self.cycles:
            raise ValueError(f"the clip starts at {time_text(start)}, after the capture's end "
                             f"({time_text(self.cycles / cps)})")
        return _Pieces(self._clip(first, last, on_damage))

    def _clip(self, first, last, on_damage):
        """clip()'s bytes, from the frames that start at capture time `first`
        (cycles) up to `last`."""
        start = self.block_at(cycles=first) if first else 0
        head, palt = self.context(start) if start else (None, None)
        pending = []  # the chunks since the last frame: they go with the next one
        taking = False
        for i in range(start, len(self.blocks)):
            try:
                raw = self.read_block(i)
            except CaptureError as e:
                if on_damage is None:
                    raise
                on_damage(i, e)
                raw = b"".join(c for b, c in self.recovery if b == i)
            t = self.blocks[i].capture_time
            pos = 0
            for tag, c in chunks_in(raw):
                pos += len(c)
                if tag != b"FRAM":
                    pending.append(c)
                    continue
                if t >= last:
                    return
                if t >= first:
                    if not taking:
                        taking = True
                        sent = {p[:4] for p in pending}  # a new HEAD or PALT comes with it
                        yield b"".join(x for x, name in ((head, b"HEAD"), (palt, b"PALT"))
                                       if x and name not in sent)
                    yield b"".join(pending) + c
                else:
                    for p in pending:
                        if p[:4] == b"HEAD":
                            head = p
                        elif p[:4] == b"PALT":
                            palt = p
                pending = []
                t += 76 * (struct.unpack_from("<H", c, 20)[0] if len(c) >= 22 else 0)
            if pos < len(raw):
                pending.append(raw[pos:])  # a stream cut off inside a chunk
        if taking:
            yield b"".join(pending)  # what follows the last frame: END, say


class _Pieces:
    """Byte strings from an iterator as a file-like object (read(n))."""

    def __init__(self, pieces):
        self._pieces = iter(pieces)
        self._buf, self._pos = b"", 0

    def read(self, n=-1):
        parts = []
        while n:
            if self._pos >= len(self._buf):
                self._buf, self._pos = next(self._pieces, None), 0
                if self._buf is None:
                    self._buf = b""
                    break
                continue
            take = len(self._buf) - self._pos if n < 0 else min(n, len(self._buf) - self._pos)
            parts.append(self._buf[self._pos:self._pos + take])
            self._pos += take
            if n > 0:
                n -= take
        return b"".join(parts)


class _RawStream:
    def __init__(self, capture, start, on_damage):
        self._capture = capture
        self._next = start
        self._on_damage = on_damage
        self._buf = b"".join(c for c in capture.context(start) if c) if start else b""
        self._pos = 0

    def _load(self, i):
        try:
            return self._capture.read_block(i)
        except CaptureError as e:
            if self._on_damage is None:
                raise
            self._on_damage(i, e)
            return b"".join(c for b, c in self._capture.recovery if b == i)

    def read(self, n=-1):
        parts = []
        while n:
            if self._pos >= len(self._buf):
                if self._next >= len(self._capture.blocks):
                    break
                self._buf = self._load(self._next)
                self._next += 1
                self._pos = 0
                continue
            take = len(self._buf) - self._pos if n < 0 else min(n, len(self._buf) - self._pos)
            parts.append(self._buf[self._pos:self._pos + take])
            self._pos += take
            if n > 0:
                n -= take
        return b"".join(parts)


def is_capture(path):
    """True if the file starts with a capture file's magic."""
    try:
        with open(path, "rb") as f:
            return f.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def parse_time(text):
    """'80', '80.5', '1:20' or '1:02:03.5' -> seconds. Raises ValueError."""
    *whole, last = text.strip().split(":")
    if (len(whole) > 2 or not all(re.fullmatch(r"[0-9]+", p) for p in whole)
            or not re.fullmatch(r"[0-9]+(\.[0-9]*)?|\.[0-9]+", last)
            or any(int(p) >= 60 for p in whole[1:]) or (whole and float(last) >= 60)):
        raise ValueError(f"not a time: '{text}' (seconds, minutes:seconds or "
                         "hours:minutes:seconds)")
    seconds = 0
    for p in whole:
        seconds = seconds * 60 + int(p)
    return seconds * 60 + float(last)


def time_text(seconds):
    """80.5 -> '1:20.50', 3723.5 -> '1:02:03.50'."""
    hundredths = round(seconds * 100)
    minutes, rest = divmod(hundredths, 6000)
    hours, minutes = divmod(minutes, 60)
    secs = f"{rest // 100:02d}.{rest % 100:02d}"
    return f"{hours}:{minutes:02d}:{secs}" if hours else f"{minutes}:{secs}"


def time_option(text):
    """parse_time() for argparse."""
    try:
        return parse_time(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _size(n):
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1000 or unit == "GB":
            return f"{n:,} bytes" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1000


def _seconds(cap):
    """Capture time in seconds, with the first HEAD's cycles per second."""
    head = cap.first_head()
    cps = struct.unpack_from("<I", head, 16)[0] if head and len(head) >= 20 else 0
    return (cap.cycles / cps if cps else None), head


def summary(writer, seconds_taken, cpu):
    """One line on a finished Writer, for the command line."""
    s = writer.cycles / writer._cps
    line = (f"{_size(writer.bytes_in)} packed into {_size(writer.bytes_out)} "
            f"({METHOD_NAMES[writer.method]}), {writer.bytes_in / max(writer.bytes_out, 1):.0f}x "
            f"smaller: {writer.blocks} blocks, {writer.frames:,} frames, {s:.2f} s")
    if s:
        line += f", {writer.bytes_out / s / 1000:.1f} KB a second"
    line += f". {seconds_taken:.1f} s, CPU {cpu:.2f} s"
    return line


def report(path, verbose=False, out=sys.stdout):
    """Print what a capture holds and decode every block. True if all pass."""
    def p(*a):
        print(*a, file=out)

    with open(path, "rb") as f:
        cap = Capture(f)
        p(f"{path}: StellaStream capture, format version {cap.version}")
        if cap.indexed:
            p(f"Blocks:   {len(cap.blocks)} (from the index)")
        else:
            p(f"Blocks:   {len(cap.blocks)} found by walking the file ({cap.index_problem})")
        seconds, head = (None, None)
        try:
            seconds, head = _seconds(cap)
        except CaptureError:
            pass
        timing = ""
        if head:
            h = next(parse(_Bytes(head)))
            timing = f" ({h.timing_name}, ROM '{h.rom_name}')"
        if seconds is not None:
            p(f"Frames:   {cap.frames:,}, {seconds:.2f} s of emulated time{timing}")
        else:
            p(f"Frames:   {cap.frames:,}, {cap.cycles:,} cycles of emulated time")
        p("XORed:    " + (", ".join(t.decode("ascii", "replace").strip() for t in cap.delta_tags)
                          or "nothing"))
        kinds = ", ".join(c[:4].decode("ascii") for _, c in cap.recovery)
        p(f"Recovery: {len(cap.recovery)} copies" + (f" ({kinds})" if kinds else ""))

        raw_total = 0
        bad = []
        if verbose:
            p("")
            p("block      offset  frames   start s        raw   stored  method  flags")
        for i, b in enumerate(cap.blocks):
            f.seek(b.offset)
            _, stored, raw_len, *_ = BLOCK_HEADER.unpack_from(f.read(BLOCK_HEADER.size))
            raw_total += raw_len
            try:
                cap.read_block(i)
            except CaptureError as e:
                bad.append(str(e))
            if verbose:
                start = f"{b.capture_time / (cap.cycles / seconds):9.2f}" if seconds else " " * 9
                flags = " ".join(name for bit, name in ((HAS_HEAD, "HEAD"), (HAS_PALT, "PALT"),
                                                        (HAS_JUMP, "JUMP"), (HAS_END, "END"),
                                                        (CUT, "cut"))
                                 if b.flags & bit)
                p(f"{i:5d} {b.offset:11,d} {b.frames:7d} {start} {raw_len:10,d} {stored:8,d}  "
                  f"{METHOD_NAMES.get(b.method, b.method)!s:6}  {flags}")
        if verbose:
            p("")
        stored_total = cap.size
        ratio = raw_total / max(stored_total, 1)
        line = f"Size:     {_size(raw_total)} of raw stream in {_size(stored_total)}, {ratio:.0f}x smaller"
        if seconds:
            line += f", {stored_total / seconds / 1000:.1f} KB a second"
        p(line)
        if bad:
            p(f"Check:    {len(bad)} of {len(cap.blocks)} blocks damaged:")
            for message in bad:
                p("          " + message)
        else:
            p("Check:    every block decodes and passes its CRC")
    return not bad


class _Bytes:
    """bytes as a minimal stream for rawstream.parse()."""

    def __init__(self, data):
        self._data, self._pos = data, 0

    def read(self, n):
        part = self._data[self._pos:self._pos + n]
        self._pos += len(part)
        return part


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", metavar="file",
                    help="a raw stream and the capture to write, a capture and the raw stream "
                         "to write, or one capture to report on")
    ap.add_argument("--rom", help="run Stella on this ROM and save its stream to the capture")
    ap.add_argument("--frames", type=int, default=0,
                    help="with --rom: ask Stella to close after this many frames")
    ap.add_argument("--silent", action="store_true",
                    help="with --rom: no sound from Stella, and its own settings folder")
    ap.add_argument("--stella", help="the Stella to run (default: found automatically)")
    ap.add_argument("--stella-args", default="",
                    help='with --rom: more Stella options, e.g. --stella-args="-lc Driving"')
    ap.add_argument("--method", choices=list(METHODS), default="xz",
                    help="compression (default xz)")
    ap.add_argument("--level", type=int,
                    help="compression level (default: zlib 6, xz 6, zstd 3)")
    ap.add_argument("--delta", default="FRAM,RAM",
                    help="chunks XORed with the one before (default FRAM,RAM), e.g. FRAM,RAM,AUDI")
    ap.add_argument("--block-seconds", type=float, default=1.0,
                    help="emulated seconds a block holds (default 1)")
    ap.add_argument("--no-recovery", action="store_true",
                    help="leave out the copies of HEAD and PALT at the end")
    ap.add_argument("--verbose", "-v", action="store_true", help="report: one line per block")
    ap.add_argument("--from", dest="start", type=time_option, metavar="TIME",
                    help="a clip of a capture: from this time (seconds, or minutes:seconds)")
    ap.add_argument("--to", dest="end", type=time_option, metavar="TIME",
                    help="a clip of a capture: up to this time (default: the end)")
    args = ap.parse_args()

    options = dict(method=METHODS[args.method], level=args.level,
                   delta_tags=[t.strip() for t in args.delta.split(",") if t.strip()],
                   block_seconds=args.block_seconds, recovery=not args.no_recovery)
    clip = args.start is not None or args.end is not None
    try:
        if args.rom:
            if len(args.paths) != 1:
                ap.error("with --rom, give only the capture to write")
            if clip:
                ap.error("--from and --to cut a clip from a capture, not from --rom")
            ok = _record(args, options)
        elif len(args.paths) == 1:
            if clip:
                ap.error("--from and --to need a capture to cut and a file to write")
            ok = report(args.paths[0], args.verbose)
        elif len(args.paths) == 2:
            source, target = args.paths
            if _same_file(source, target):
                ap.error("the file to write is the one being read")
            suffix = os.path.splitext(target)[1].lower()
            if suffix in VIDEO_SUFFIXES:
                what = "a GIF" if suffix == ".gif" else "a video"
                ap.error(f"sscap.py writes captures and raw streams, not videos or GIFs. For "
                         f"{what}: python record.py {source} {target}"
                         + (" (with the same --from and --to)" if clip else ""))
            if is_capture(source):
                ok = _from_capture(source, target, args, options)
            elif clip:
                ap.error("--from and --to cut a clip from a capture (.sscap); pack the "
                         "stream first")
            else:
                start, cpu = time.monotonic(), time.process_time()
                with open(source, "rb") as f, open(target, "wb") as out, \
                        Writer(out, **options) as w:
                    while part := f.read(1 << 20):
                        w.write(part)
                print(summary(w, time.monotonic() - start, time.process_time() - cpu),
                      file=sys.stderr)
                ok = True
        else:
            ap.error("give one or two files")
    except (CaptureError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if ok else 1)


def _same_file(a, b):
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False  # one of them isn't there yet


def _from_capture(source, target, args, options):
    """Unpack a capture, or a clip of it (--from, --to), into a raw stream;
    or pack it into a new capture when target ends in .sscap. True if it
    holds a frame."""
    start, cpu = time.monotonic(), time.process_time()
    clip = args.start is not None or args.end is not None
    packing = target.lower().endswith(".sscap")
    with open(source, "rb") as f:
        cap = Capture(f)
        if clip:
            stream = cap.clip(args.start or 0.0, args.end)
            length = cap.seconds()
            end = length if args.end is None else min(args.end, length)
            what = f"the clip from {time_text(args.start or 0.0)} to {time_text(end)}"
        else:
            stream, what = cap.stream(), f"{len(cap.blocks)} blocks"
        taken = 0
        with open(target, "wb") as out:
            w = Writer(out, **options) if packing else out
            while part := stream.read(1 << 20):
                w.write(part)
                taken += len(part)
            if packing:
                w.close()
            size = out.tell()
    if clip and not taken:
        os.remove(target)
        print(f"Error: no frame starts in {what}", file=sys.stderr)
        return False
    if not packing:
        print(f"Unpacked {what} into {_size(size)}", file=sys.stderr)
    else:
        if clip:
            print(f"Packed {what} of {source}", file=sys.stderr)
        print(summary(w, time.monotonic() - start, time.process_time() - cpu), file=sys.stderr)
    return True


def _record(args, options):
    """Run Stella and save its stream until it closes. True if a stream came."""
    proc = launch_stella(args.rom, extra_args=args.stella_args.split(), silent=args.silent,
                         own_settings=args.silent, stella=args.stella)
    closer = None
    start, cpu = time.monotonic(), time.process_time()
    try:
        with open(args.paths[0], "wb") as out, Writer(out, background=True, **options) as w:
            try:
                while data := proc.stdout.read(1 << 16):
                    w.write(data)
                    if args.frames and w.frames >= args.frames and closer is None:
                        # Keep reading while Stella closes, so it can send END
                        closer = threading.Thread(target=close_stella, args=(proc,), daemon=True)
                        closer.start()
            except KeyboardInterrupt:
                print("Stopped", file=sys.stderr)
    finally:
        proc.stdout.close()
        if closer:
            closer.join()
        close_stella(proc)
    if not w.bytes_in:
        print("Stella sent no stream. Is it a Stella with -rawstream, and does the ROM load?",
              file=sys.stderr)
        return False
    print(summary(w, time.monotonic() - start, time.process_time() - cpu), file=sys.stderr)
    return True


if __name__ == "__main__":
    main()
