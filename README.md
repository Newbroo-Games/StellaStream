<a href="https://newbroo.com"><img src="images/logo.png" alt="Newbroo Games" width="300"></a>

# StellaStream

Record Atari 2600 games played in the [Stella](https://stella-emu.github.io/)
emulator to MP4 videos, frame-perfect and with the sound in sync.

![Four seconds of a test ROM, recorded with StellaStream](images/demo.gif)

*A test ROM made for this page, as StellaStream records it (a GIF here; the
video has the tune too).*

It uses Stella's `-rawstream` option, which hands every emulated frame, audio
sample and the console's RAM, untouched, to an outside program. StellaStream is
one example of what that stream makes possible.

Tested on Windows. The code is plain Python and should work on macOS and
Linux too, but nobody has tried them yet.

## Needs

- Python 3.10 or newer, with tkinter (the python.org installers include it).
  Nothing else to install from pip.
- [ffmpeg](https://ffmpeg.org/), on the PATH or chosen in the window, for
  videos (captures and GIFs don't need it).
- A Stella build with `-rawstream`. Until Stella itself has it, build the
  `rawstream-7.0c` branch of
  [Newbroo-Games/stella](https://github.com/Newbroo-Games/stella/tree/rawstream-7.0c):
  Stella 7.0c plus the option, described in its
  [docs/rawstream.md](https://github.com/Newbroo-Games/stella/blob/rawstream-7.0c/docs/rawstream.md).

StellaStream looks for Stella in this order: the `STELLASTREAM_STELLA`
environment variable, a Stella source checkout built next to this folder
(`../stella`), then Stella on the PATH. The window's Stella program field (under
More options) and the command line's `--stella <path>` override all three.

## Record from the window

```
python stellastream.pyw
```

On Windows, double-clicking `stellastream.pyw` works too.

![The window while it records](images/window.png)

1. Choose the game's ROM. The video and the capture are named after it and
   saved next to it, until you name them something else.
2. If Stella can't tell, choose the controllers and the TV format (a driving
   game needs Driving, for example).
3. Choose what to save: a **Video**, a **Capture** (the whole stream in a small
   file, see [Capture files](#capture-files)), or **Both**. A capture alone
   needs no ffmpeg and little CPU while you play; make videos from it later.
4. Press **Record** and play in Stella's window. **Stop**, closing Stella, or
   the time limit ends the recording, and the files are finished.
5. **Open video** plays the video; **Show folder** shows where the files are.

Good to know:

- A file is never replaced: if the name is taken, the new one becomes
  `game-2.mp4`, `game-3.mp4`, and so on; a video and capture saved together
  keep the same number.
- Closing the window while recording stops the recording and saves the files
  first.
- With **Hear the game** off, Stella plays silently; the files have the sound
  either way, because it comes from the stream.
- The **Stella command** box shows exactly what the window runs, so you can
  see the option at work, or copy it.
- **More options:** the Stella and ffmpeg programs, extra Stella options, the
  video quality, keeping the separate picture and sound files, and **Sync**
  (see [Picture and sound together](#picture-and-sound-together)).
- **Video or GIF from a capture:** choose a capture, type the part you want
  (**From** and **To**, in minutes:seconds; leave them empty for the start
  and the end) and press **Make video**. The video is named after the
  capture (`game-clip.mp4`) and has the size, stereo sound, quality and sync
  set for recording. Name it `.gif` instead and the button becomes **Make GIF**
  (see [Animated GIFs](#animated-gifs)).
- Every field is remembered, in `%APPDATA%\StellaStream\settings.json` on
  Windows and `~/.config/StellaStream/settings.json` elsewhere.

## Record from the command line

Record while you play; close Stella to finish the video:

```
python record.py --rom path/to/game.bin game.mp4
```

Options: `--scale 2` for 1280-wide video, `--stereo`, `--seconds 60` to stop
after a minute, `--stella-args="-lc Driving"` for any other Stella options
(here, a driving controller on the left port), `--ffmpeg <path>` if ffmpeg
isn't on the PATH, `--capture game.sscap` to save a capture of the same play
too, `--sync picture` or `--sync sound` (see below). Ctrl+C also stops and
finishes the video.

Record from a stream saved earlier with `Stella -rawstream capture.raw game.bin`:

```
python record.py capture.raw game.mp4
```

## Picture and sound together

Most games draw 262 lines a frame (312 on PAL), exactly 60 (50) frames a
second, and for them every Sync choice gives the same video. A game that draws
more or fewer lines runs at a slightly different rate, and a video has to give
a little somewhere:

- **Smooth** (the default): every picture once, at 60 a second (PAL: 50). The
  sound is stretched or squeezed slightly to fit, eased over a quarter of a
  second so the pitch stays steady. A 263-line game plays 0.4% fast, a
  272-line one 3.8% (a little higher in pitch). Flicker looks right on a
  normal 60 Hz screen.
- **Picture**: every picture once, at the game's own rate (263 lines: 59.77
  frames a second), so it plays at the real speed with every sound sample. If
  the game's line count changes from frame to frame, each frame's sound is
  cut or padded to fit, which can sound a little rough. A 60 Hz screen shows
  some pictures twice while it plays such a video.
- **Sound**: every sound sample at the real speed, at 60 frames a second; now
  and then a picture is shown twice or left out to keep up (263 lines: one
  every 4 seconds). That breaks a flicker effect for a frame.

In every case each picture's sound stays under it, within a few milliseconds
(Sound: within half a frame). GIFs aren't affected: they always run at 50.

## Capture files

A capture file (`.sscap`) keeps the whole stream (every frame, every sound
sample, the RAM) packed in one-second blocks: in our tests about 300 times
smaller than the raw stream, some 7 KB a second or 25 MB an hour. Make videos
from it whenever you like.

Save one while you play; close Stella to finish:

```
python sscap.py --rom path/to/game.bin game.sscap
```

Make a video from it:

```
python record.py game.sscap game.mp4
```

Cut a clip, as a video or as a smaller capture. The times are the same as in
the whole video, in seconds or minutes:seconds; leave out `--to` to go to the
end:

```
python record.py game.sscap clip.mp4 --from 1:20 --to 1:45
python sscap.py game.sscap clip.sscap --from 1:20 --to 1:45
```

Pack a raw stream, unpack a capture again, or check one (`-v`: a line per
block):

```
python sscap.py capture.raw game.sscap
python sscap.py game.sscap capture.raw
python sscap.py game.sscap -v
```

The format is described in [docs/capture-format.md](docs/capture-format.md).

## Animated GIFs

Name the file `.gif` and you get an animated GIF of the capture, or of a
clip of it:

```
python record.py game.sscap clip.gif --from 1:20 --to 1:25
```

Every pixel keeps Stella's own color (nothing dithered or blurred), drawn in
the TV shape, 640 wide. A GIF can't show 60 frames a second (browsers slow
down anything faster than 50), so it runs at 50: from an NTSC game one frame
in six is left out, a PAL game keeps them all. Only the part of each frame
that changed is stored, which keeps the files small. A GIF has no sound, and
StellaStream writes it itself: no ffmpeg needed. A raw stream file works too;
a recording with `--rom` doesn't (record a capture, then make the GIF from
it).

## Check a stream

Check a stream, and print RAM bytes every frame (addresses, or names from a
DASM `.sym` file):

```
python rawstream.py --rom path/to/game.bin --frames 300 --watch Frame,$b6 --sym game.sym
```

## Tests

```
python -m unittest discover -s tests -t .
```

The tests build their own tiny 2600 ROM (`tests/testrom.py`), run Stella on it
silently with a settings folder of their own, and check the stream, the RAM
against the picture, the videos, and capture files (a round trip byte for
byte, damaged files, the format spec's own decoder), and GIFs (read back by a
GIF reader of the tests' own, and by ffmpeg). The window's tests build it
hidden, on a settings file of their own, and record that ROM from it. Without
Stella or ffmpeg those tests are skipped.

## License

MIT; see [LICENSE](LICENSE). The Newbroo Games name and logo are not covered
by it.
