# video-converter

Batch conversion tool for Canon Log3 / Cinema Gamut footage to H.264 MP4, with automatic LUT application, exposure analysis, slow-motion export, scrubber thumbnail generation, and `manifest.json` output for the [video-sharing](../video-sharing) web application.

---

## Requirements

| Tool | Purpose |
|------|---------|
| `ffmpeg` / `ffprobe` | Video conversion, thumbnail extraction, sprite generation |
| Python 3.9+ | Runs `convert.py` |
| macOS with VideoToolbox | Hardware H.264 encoding (`h264_videotoolbox`) |

Install ffmpeg via Homebrew:
```bash
brew install ffmpeg
```

---

## Directory Layout

```
video-converter/          ← this repo
  convert.py
  *.cube                  ← LUT files
video-sharing/            ← sibling repo (optional — not required by convert.py)
  server.js
  src/
```

`convert.py` is fully self-contained. All thumbnail generation and manifest writing logic is inlined — no dependency on any file in `video-sharing`.

---

## Usage

```bash
./convert.py --opponent <name> [OPTIONS]
# or
python3 convert.py --opponent <name> [OPTIONS]
```

### Required

| Flag | Description |
|------|-------------|
| `--opponent <name>` | Opposing team name, used in output filenames and directory |

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--input-dir <path>` | `/Volumes/EOS_DIGITAL/DCIM/100EOSR7` | Source directory containing `.mp4` / `.mov` files |
| `--output-dir <path>` | `/Volumes/Data/Videos/Lacrosse` | Base output directory; a `<opponent>-<date>` subfolder is created automatically |
| `--lut <file>` | `CinemaGamut_CanonLog3-to-Canon709_33_Ver.1.0.cube` | Path to a `.cube` LUT file |
| `--target-fps <fps>` | `60` | Frame rate for the slow-motion export pass |

### Examples

```bash
# Basic usage — reads from camera card, writes to default output dir
./convert.py --opponent Loyola

# Custom input/output
./convert.py --opponent Loyola --input-dir ~/Desktop/footage --output-dir ~/Movies

# Override LUT and slow-mo target frame rate
./convert.py --opponent Loyola --lut ~/luts/custom.cube --target-fps 30
```

---

## Output Files

For each source clip the script produces up to **five files** in `<output-dir>/<opponent>-<date>/`:

| File | Description |
|------|-------------|
| `<base>_<src_fps>fps.mp4` | **Primary** — full frame-rate H.264 with LUT applied |
| `<base>_thumb.jpg` | **Card thumbnail** — 480 px wide JPEG at 1 s, used by the webapp grid view |
| `<base>_<src_fps>fps_sprite.jpg` | **Scrubber sprite sheet** — 160 px wide tiles at 2 s intervals |
| `<base>_<src_fps>fps_thumbnails.vtt` | **WebVTT** — maps playback time → sprite coordinates for the player scrubber |
| `<base>_<target_fps>fps.mp4` | **Slow-motion** — only when `src_fps > target_fps` (e.g. 120 fps → 60 fps) |

When a slow-motion file is produced, its own `_sprite.jpg` and `_thumbnails.vtt` are also generated.

After all clips are processed, a `manifest.json` is written into the game folder so the video-sharing webapp can serve the clips immediately without any additional steps.

### Naming convention

```
<opponent_slug>_<YYYY-MM-DD>_<NNN>_<fps>fps
```

- `opponent_slug` — lowercase, spaces → underscores, special characters stripped
- `YYYY-MM-DD` — recording date extracted from the clip's metadata
- `NNN` — zero-padded clip index (001, 002, …)

**Example set for one 120 fps clip:**
```
loyola_2026-03-18_001_120fps.mp4
loyola_2026-03-18_001_thumb.jpg
loyola_2026-03-18_001_120fps_sprite.jpg
loyola_2026-03-18_001_120fps_thumbnails.vtt
loyola_2026-03-18_001_60fps.mp4
loyola_2026-03-18_001_60fps_sprite.jpg
loyola_2026-03-18_001_60fps_thumbnails.vtt
manifest.json
```

---

## Processing Details

### Exposure Analysis

Before each conversion the script samples the source clip at 25 %, 50 %, and 75 % of its duration using `signalstats` to measure average luminance (YAVG). It normalises the value to an 8-bit scale (handles 10-bit / 12-bit sources) and calculates the stops of adjustment needed to reach a target of 105 (punchy sports exposure). Adjustments smaller than ±0.15 stops are ignored; values are clamped to ±2.0 stops.

### Parallel Processing

Up to 4 clips are processed concurrently using `ThreadPoolExecutor`. Metadata (duration, fps, resolution, creation date) is read synchronously before the pool starts to guarantee correct clip numbering.

### Slow-Motion Export

When the source frame rate exceeds `--target-fps`, a second pass selects every Nth frame (`120 / target_fps`) and resets the presentation timestamps so the clip plays back at `target_fps` — producing true slow motion without interpolation.

### Manifest Generation

After all clips finish, `manifest.json` is written into the game folder. It groups MP4 files by clip, records duration, fps, and stretch factor for each version, and preserves any `tags` that were already present in a prior `manifest.json`. This file is consumed directly by the video-sharing webapp's `server.js`.

---

## LUT Files

Two LUTs are included:

| File | Use |
|------|-----|
| `CinemaGamut_CanonLog3-to-Canon709_33_Ver.1.0.cube` | Canon Cinema Gamut / Log3 → Rec.709 (default) |
| `CINECOLOR_CANON_EOS_C-LOG.cube` | Canon C-Log alternative |

Pass a custom LUT with `--lut <path>`.
