# VideoCompressor

Drop a video in, get a smaller one back. One screen, no settings.

It measures how wastefully your video was encoded, finds the smallest file that
still looks identical, and never hands back something bigger than what you gave
it. Typical reductions on camera and phone footage are **80–90%**.

---

## Running it on a new laptop

Everything below is required. Steps 1 and 2 are the only ones that ever cause
trouble, and step 4 tells you immediately if either went wrong.

### 1. Install Python 3.9 or newer

Check whether you already have it:

```bash
python3 --version        # macOS / Linux
python --version         # Windows
```

If that prints 3.9 or higher, skip ahead.

| OS | How |
|---|---|
| **macOS** | `brew install python` — or download from [python.org](https://www.python.org/downloads/) |
| **Windows** | Download from [python.org](https://www.python.org/downloads/). **Tick "Add Python to PATH"** on the first screen of the installer — if you miss it, nothing below will work |
| **Linux** | `sudo apt install python3 python3-pip` (Debian/Ubuntu) or `sudo dnf install python3 python3-pip` (Fedora) |

### 2. Install FFmpeg — the important one

This app is a front end over FFmpeg. Without it, nothing runs.

| OS | How |
|---|---|
| **macOS** | `brew install ffmpeg` (install [Homebrew](https://brew.sh) first if needed) |
| **Windows** | `winget install Gyan.FFmpeg` in PowerShell. Then **close and reopen the terminal** so PATH updates |
| **Linux** | `sudo apt install ffmpeg` or `sudo dnf install ffmpeg` |

Verify:

```bash
ffmpeg -version
```

If it prints version info, you're set. If it says "command not found", FFmpeg
isn't on your PATH — see [Troubleshooting](#troubleshooting).

> **Windows note:** the `winget` build above includes everything needed. If you
> download FFmpeg manually, get a **full/GPL build** (gyan.dev or BtbN), not the
> "essentials" build — the smaller ones ship without the quality-measurement
> library, and the app will fall back to estimating quality instead of measuring
> it.

### 3. Get the project and install its one dependency

```bash
cd path/to/VideoCompressor
pip install -r requirements.txt
```

The only Python dependency is Flask. Everything else is FFmpeg.

<details>
<summary>Using a virtual environment (recommended, optional)</summary>

Keeps this project's packages separate from the rest of your system:

```bash
# macOS / Linux
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Windows
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

You need to run the `activate` line again each time you open a new terminal.
</details>

### 4. Check the setup before running

```bash
python video_compressor.py check
```

This is the single most useful command on a new machine. Expected output:

```
  FFmpeg   /opt/homebrew/bin/ffmpeg
           ffmpeg version 7.1.1 ...
  VMAF     available
  Codecs
    [yes] H.265 / HEVC     Plays natively on macOS, iOS, Windows 10+, Android 5+.
    [yes] AV1              Smallest files. Slower to encode; needs a recent player.
    [yes] H.264 / AVC      Plays on anything ever made.
```

- **`VMAF available`** — quality is measured. This is what you want.
- **`VMAF MISSING`** — the app still works, but it *estimates* quality instead
  of measuring it. Install a fuller FFmpeg build to fix.
- **At least one `[yes]` codec** is required. All three is normal.

### 5. Run it

```bash
python app.py
```

Then open **<http://127.0.0.1:5001>** in your browser.

Drag a video onto the page. That's the whole interface.

To stop the server, press `Ctrl+C` in the terminal.

---

## What you'll see

**Idle** — a drop zone. Nothing else.

**Working** — one progress bar and one line: *Analyzing… → Finding the best
quality… → Compressing…*

**Done** — one line, e.g. *"Reduced by 82% (VMAF 95)"*, and a Download button.

**Occasionally** — *"This file is already efficient — compressing it further
would make it worse, so nothing was changed."* This is not an error. It means
your file is already well compressed, and re-encoding it would produce a
**larger** file with worse quality. Keeping the original is the right answer.
There's a **Compress anyway** button if you want it regardless.

Two lines appear only when relevant:

- *"Compressed quickly using hardware acceleration…"* — the video was long
  enough that software encoding wouldn't finish in a reasonable time.
- *"Saved as .mp4 — your original format doesn't support this codec."* — e.g. a
  `.mov` compressed with AV1, which QuickTime's container can't hold.

---

## Troubleshooting

**"FFmpeg was not found"**

The app searches your PATH and the usual install locations. If FFmpeg lives
somewhere unusual, point at it directly:

```bash
# macOS / Linux
export FFMPEG_BINARY=/path/to/ffmpeg
export FFPROBE_BINARY=/path/to/ffprobe

# Windows (PowerShell)
$env:FFMPEG_BINARY="C:\path\to\ffmpeg.exe"
$env:FFPROBE_BINARY="C:\path\to\ffprobe.exe"
```

**"Port 5001 is in use"**

Either something else is using it, or a previous run is still going:

```bash
PORT=5055 python app.py           # macOS / Linux
$env:PORT=5055; python app.py     # Windows
```

**Windows: `python` is not recognised**

Python wasn't added to PATH during install. Re-run the installer, choose
"Modify", and tick "Add Python to PATH".

**It's slower than expected**

Encoding is CPU-bound, and this app deliberately favours smaller files over
speed. Rough guide on a modern laptop: a 1-minute 1080p video takes 1–3 minutes.
Longer videos switch to faster settings automatically, and very long ones use
hardware acceleration.

**The result was bigger, so nothing happened**

Working as intended — see the refusal note above.

---

## How it works

Most compressors apply a fixed preset and hand you whatever comes out. But the
size available in a video isn't a property of the encoder — it's a property of
how wastefully the source was encoded to begin with.

1. **Analyse.** Measure bits per pixel per frame. A phone recording sits near
   0.10–0.20; a well-encoded download near 0.02. The first has huge headroom,
   the second almost none.
2. **Search.** Encode short samples from across the video at different quality
   settings, score each against the original with
   [VMAF](https://github.com/Netflix/vmaf) (Netflix's perceptual quality
   metric), and binary-search for the smallest file that still scores 95 — the
   level where differences aren't perceptible in normal viewing.
3. **Encode once** at that setting, then verify the result really is smaller.

Three guards make sure you never get a worse file: the analysis predicts
available savings before encoding, the samples project the output size and abort
if it would grow, and a finished encode that is somehow still larger is
discarded.

Everything is chosen for you: quality floor 95, the best codec your FFmpeg
supports, encoder effort matched to the video's length, audio copied through
untouched when it's already efficient, and resolution preserved.

---

## Command line (optional)

The web app is the main interface, but the same engine has a CLI:

```bash
python video_compressor.py check                  # verify the setup
python video_compressor.py info holiday.mov       # analyse without encoding
python video_compressor.py compress holiday.mov   # -> holiday_compressed.mp4
python video_compressor.py batch ./clips ./out    # a whole folder
```

`info` is handy for deciding whether a file is worth compressing — it prints the
measured bpp and expected savings in a second or two, without encoding anything.

Useful flags: `--codec {h265,av1,h264}`, `--vmaf N` (quality floor),
`--speed {quality,balanced,fast}`, `--max-height 1080`, `--even-if-bigger`.
Run `python video_compressor.py compress --help` for the full list.

---

## Project layout

```
app.py             Web server: upload, job queue, REST API
upload_store.py    Resumable uploads (survive a dropped connection)
compressor/
  environment.py   Finds FFmpeg, detects what it can do
  probe.py         Source analysis, the bits-per-pixel waste metric
  encoders.py      Codec definitions and FFmpeg arguments
  quality.py       VMAF sampling and the quality search
  segmenter.py     Scene detection and per-scene encoding
  engine.py        Orchestration, progress, cancellation
  cli.py           Command-line interface
templates/         The one page
static/            Its stylesheet and logic
```

`uploads/` and `outputs/` are created automatically and are safe to delete when
the app isn't running.

---

## Notes

- **Runs entirely on your machine.** Nothing is uploaded anywhere; the server
  binds to localhost only.
- **Your originals are never modified or deleted.**
- **Large files are fine.** Uploads stream to disk in chunks and resume after a
  dropped connection — a 1.19 GB upload uses about 2 MB of memory.
