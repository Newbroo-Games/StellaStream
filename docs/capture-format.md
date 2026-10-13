# StellaStream capture files

*Format version 1.*

A capture file holds a raw stream (the output of Stella's `-rawstream`, see
the fork's
[`docs/rawstream.md`](https://github.com/Newbroo-Games/stella/blob/rawstream-7.0c/docs/rawstream.md))
compressed in self-contained blocks, with an index at the end. A stored session
then costs kilobytes a second instead of the raw stream's 2.2 MB, and any point
in it can be reached by decoding one block, not everything before it.

Decoding a whole file gives back the raw stream byte for byte, unknown chunks
included. So everything the raw stream spec says holds for the decoded data,
and this file only describes the packing.

Stella's own output doesn't change: the stream leaves Stella raw, and a writer
such as StellaStream packs it when saving.

The file extension is `.sscap` (StellaStream capture).

## Layout

```
file header     what the file is, which chunks are delta-coded
block 0         about one second of the stream, compressed on its own
block 1
...
block n-1
index           one entry per block, then the recovery copies
                                              (missing if the writer was cut off)
trailer         where the index is, 16 bytes  (missing if the writer was cut off)
```

All numbers are little-endian, as in the raw stream. Tags are four ASCII
characters.

## File header

| Offset | Bytes | Field |
|---|---|---|
| 0 | 8 | Magic: `89 53 53 43 0D 0A 1A 0A` (`\x89SSC\r\n\x1a\n`) |
| 8 | 2 | Format version (1) |
| 10 | 2 | Header length: bytes from the start of the file to the first block (16 + 4 x n) |
| 12 | 1 | Block header length (36) |
| 13 | 1 | Index entry length (24) |
| 14 | 1 | n: number of delta-coded tags |
| 15 | 1 | 0 |
| 16 | 4 x n | The delta-coded tags, for example `FRAM` and `RAM ` |

The magic works like PNG's: the first byte isn't ASCII, and a file sent through
a text-mode transfer gets its CR, LF and Ctrl-Z bytes changed, which shows.

A reader refuses a version it doesn't know. A later version that only adds
fields at the end of the file header, a block header or an index entry keeps
the version number; readers use the three lengths to skip fields they don't
know.

## Blocks

A block is a run of whole raw-stream chunks, compressed on its own. Readers
must accept any cut: a block can hold any number of frames, none included.
StellaStream closes a block after the frame that brings it to one second of
emulated time (60 frames NTSC, 50 PAL), and closes the last one when the
stream ends. Longer blocks make a slightly smaller file, shorter ones a quicker
jump: on four minutes of play, one-second blocks took 7.2 KB a second and 23 ms
of CPU to decode, two-second blocks 6.7 KB and 47 ms.

### Block header (36 bytes)

| Offset | Bytes | Field |
|---|---|---|
| 0 | 4 | Marker `SSCB` |
| 4 | 4 | Stored length: bytes of block data after this header |
| 8 | 4 | Raw length: bytes of raw stream the block gives back |
| 12 | 4 | CRC-32 of those raw stream bytes (the CRC of zlib, PNG and Ethernet) |
| 16 | 1 | Compression method, below |
| 17 | 1 | Flags, below |
| 18 | 2 | Frames: `FRAM` chunks in this block |
| 20 | 4 | Frame index: `FRAM` chunks in the file before this block |
| 24 | 8 | Capture time: emulated CPU cycles in the file before this block |
| 32 | 4 | Cycles in this block |

**Capture time** counts from the start of the file: each frame lasts 76 x its
scanline count cycles (the `FRAM` field), so a block's cycles are the sum over
its frames, and seconds are cycles / the `HEAD`'s cycles per second (1,194,720
for NTSC). Stella's own frame number and cycle count are still in each `FRAM`,
but they jump on a reset or a state load (rewind included); the capture time
always runs forward, so an index by time works across those jumps.

A block holds at most 65,535 frames and fewer than 2^32 cycles (an hour of
NTSC), and gives back less than 4 GB.

### Compression methods

| Value | Method |
|---|---|
| 0 | Stored: the block data is the delta-coded raw bytes as they are |
| 1 | zlib (RFC 1950: what `zlib.compress()` writes) |
| 2 | xz (one .xz stream; StellaStream writes it with no check of its own, since the block has its CRC) |
| 3 | Zstandard (one frame, RFC 8878) |
| 4-127 | Not used yet; kept for methods added later |
| 128-255 | Private: a writer's own experiments, with a compressor of its choice |

A reader refuses a block whose method it doesn't know. A private method can
be read only by a program that knows that compressor; StellaStream refuses
them.

StellaStream writes xz by default. Python has xz (the `lzma` module) and zlib in its standard library; Zstandard
only from Python 3.14 (`compression.zstd`), so a file written with it can't be
read by an older Python.

### Flags

| Bit | Meaning |
|---|---|
| 0 | The block holds a `HEAD` |
| 1 | The block holds a `PALT` |
| 2 | The block holds a `JUMP` |
| 3 | The block holds an `END ` |
| 4 | The block ends inside a chunk: the stream was cut off there (only the last block) |

Other bits are 0; readers ignore them. To show a frame from block k, a reader
needs the `HEAD` and `PALT` in effect there: they are in the last block at or
before k with bit 0, and with bit 1, set. If that block is damaged, the
index's recovery copies hold them (see Index).

### Inside a block

Decompressed, the block data is exactly raw length bytes long and is laid out
like the raw stream: the same chunks in the same order, each tag and length
unchanged. Only the payloads of the delta-coded tags (listed in the file
header; StellaStream delta-codes `FRAM` and `RAM `, since sound compresses
better as it is) differ:

- The first chunk of a delta-coded tag in a block is stored as it is. For
  `FRAM` this is the key frame.
- Each later chunk of that tag is stored XORed, byte by byte, with the raw
  payload of the previous chunk of the same tag in the same block, when the
  two payloads have the same length. When the lengths differ, it is stored as
  it is.

In a game most pixels stay the same from one frame to the next, so the XOR is
mostly zero bytes, which compress very well. zlib can't find the previous
frame on its own: its 32 KB window is shorter than one 33.6 KB frame. xz's
window (8 MB at preset 6) does reach it, but searching that far costs time:
on four minutes of play, xz without the XOR made a file 11% bigger and took
about 60% more CPU.

To decode, walk the chunks and keep the last raw payload of each delta-coded
tag. A chunk of that tag with a payload of the same length is XORed with it;
the result is the raw payload, and it becomes the new last one. Other chunks,
unknown tags included, are copied as they are.

Only a block with flag bit 4 may end inside a chunk; its bytes after the last
whole chunk are copied as they are.

After decoding, the CRC-32 of the result must match the block header's.

## Index

Written after the last block, when the stream has ended.

| Bytes | Field |
|---|---|
| 4 | Marker `SSCI` |
| 4 | Number of blocks |
| 4 | Total frames in the file |
| 8 | Total capture time in cycles |
| 4 | Number of recovery copies (0 if the writer left them out) |
| 24 each | One entry per block, in file order |
| | The recovery copies, below |

Each entry:

| Bytes | Field |
|---|---|
| 8 | File offset of the block header |
| 4 | Frame index (as in the block header) |
| 8 | Capture time (as in the block header) |
| 2 | Frames (as in the block header) |
| 1 | Flags (as in the block header) |
| 1 | Compression method (as in the block header) |

### Recovery copies

Every frame after a `HEAD` or `PALT` needs it (see Flags), so a damaged block
0 would leave the whole file without its colours. The recovery copies guard
against that: a copy of every whole `HEAD` and `PALT` chunk in the file, in
file order, each one:

| Bytes | Field |
|---|---|
| 4 | Number of the block the chunk is in (0 is the first block) |
| 8 + n | The chunk as in the raw stream: tag, length, payload |

A reader uses them only when a block with flag bit 0 or 1 can't be decoded
or fails its CRC: the copies with that block's number stand in for its
`HEAD` and `PALT`. Decoding the whole file never uses them, so it still gives
back the raw stream byte for byte.

They are optional (StellaStream has a setting for them); a file without them
is complete. They cost about 1 KB for each `HEAD` and `PALT`, and most files
have one of each.

## Trailer (the last 16 bytes)

| Bytes | Field |
|---|---|
| 8 | File offset of the index |
| 4 | CRC-32 of the index: all of it, from `SSCI` to the trailer, recovery copies included |
| 4 | Marker `SSCE` |

## Reading

- **The whole stream:** decode the blocks in order, from the end of the file
  header to the index (or to the end of the file). Joined, they are the raw
  stream. A block that fails its CRC is damaged; the blocks around it are
  still good.
- **One moment:** read the trailer and the index, find the last block whose
  frame index or capture time is at or before the one wanted, decode it, and
  walk its frames. Decode the blocks holding the `HEAD` and `PALT` in effect
  too (see Flags), or take them from the recovery copies if such a block is
  damaged.
- **A file without a trailer** (the writer was cut off, by a crash say): walk
  the block headers from the end of the file header, skipping each block's
  data by its stored length. The readable part ends at the first block that
  doesn't start with `SSCB` or doesn't fit in the file. Each block's CRC is
  checked when it is decoded, as in a file with an index. A tool can then
  append an index and a trailer to repair the file.

## Writing

Write the file header first. Collect chunks until a block is due, then
delta-code and compress them, and write the block header and data together.
Keep a copy of each whole `HEAD` and `PALT` as it passes. When the stream
ends, write the last block, then the index (with the recovery copies, unless
they are turned off) and the trailer. A writer that is cut off loses only the
block it was collecting.

A program can read a file while it is being written: the blocks up to the
last whole one are complete.

## A reference decoder

The block decoding in Python (standard library only):

```python
import lzma
import struct
import zlib


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
```

XORing a whole payload as one integer is what makes this fast in Python: a
second of frames takes about 17 ms this way, against about 300 ms a byte at a
time.
