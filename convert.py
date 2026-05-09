#!/usr/bin/env python3
"""
convert.py — Canon Log3 footage converter for the video-sharing web application.

Converts Canon Log3 / Cinema Gamut source clips to H.264 MP4 with LUT applied,
generates slow-motion exports for high-speed footage, produces card thumbnails
and scrubber sprite sheets / WebVTT files, then writes a manifest.json for the
output game folder so the video-sharing webapp can serve the clips immediately.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
DEFAULT_INPUT_DIR   = "/Volumes/EOS_DIGITAL/DCIM/100EOSR7"
DEFAULT_OUTPUT_DIR  = "/Volumes/Data/Videos/Lacrosse"
DEFAULT_LUT         = "CinemaGamut_CanonLog3-to-Canon709_33_Ver.1.0.cube"
DEFAULT_TARGET_FPS  = 60
MAX_JOBS            = 4   # parallel clip workers

# Scrubber sprite settings (matches generate_thumbnails.py defaults)
SPRITE_INTERVAL = 2    # seconds between thumbnail frames
SPRITE_WIDTH    = 160  # px wide per tile
SPRITE_COLUMNS  = 10   # columns in the sprite grid

# Exposure analysis — all measurements are on the raw Canon Log3 source (pre-LUT).
#
# Canon Log3 10-bit signal levels (legal range 64–940 out of 1023):
#   Middle grey  ≈ 363  (35.5 % of 1023)
#   18 % grey    ≈ 351
#   White clip   ≈ 940
#
# signalstats YAVG is reported in the native pixel range of the decoded frame:
# 0–1023 for 10-bit, 0–255 for 8-bit.  The target is scaled to match.
#
# Target: YAVG ≈ 363 (Log3 middle grey in 10-bit).  We only nudge clips that
# are meaningfully off — EXPOSURE_MIN_ADJ stops is the dead-band.
#
# The gain is applied via a ``lut`` filter that multiplies each Y (luma) value
# by the gain factor BEFORE the 3D LUT.  This operates directly on encoded
# pixel values with no colour-space conversion — the correct approach for
# log-encoded footage.  The ffmpeg ``exposure`` filter must NOT be used here
# because it linearises the signal first, which produces incorrect results on
# log-encoded footage and conflicts with the LUT's expected input range.
LOG3_MIDGREY_10BIT  = 363   # Canon Log3 middle grey in 10-bit (≈ 35.5 % of 1023)
EXPOSURE_MIN_ADJ    = 0.20  # stops — ignore adjustments smaller than this
EXPOSURE_MAX_ADJ    = 1.5   # stops — clamp to this range (conservative for safety)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description=(
            "Convert Canon Log3 footage from a camera card to H.264 MP4 with LUT applied.\n"
            "Clips are named automatically using the opponent name, recording date, and clip\n"
            "number. High-speed 1080p clips (assumed 120 fps) are also exported at the\n"
            "target frame rate as a slow-motion version."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "OUTPUT NAMING:\n"
            "  Pass 1 (primary):  <opponent>_<YYYY-MM-DD>_<NNN>_<src_fps>fps.mp4\n"
            "  Pass 2 (slow-mo):  <opponent>_<YYYY-MM-DD>_<NNN>_<target_fps>fps.mp4\n"
            "    (Pass 2 only runs when source fps > --target-fps)\n\n"
            "EXAMPLES:\n"
            f"  {Path(sys.argv[0]).name} --opponent Loyola\n"
            f"  {Path(sys.argv[0]).name} --opponent Loyola --target-fps 30\n"
            f"  {Path(sys.argv[0]).name} --opponent Loyola --input-dir ~/Desktop/footage --output-dir ~/Movies\n"
            f"  {Path(sys.argv[0]).name} --opponent Loyola --lut ~/luts/custom.cube --target-fps 30\n\n"
            "DEPENDENCIES:\n"
            "  ffmpeg / ffprobe   https://ffmpeg.org"
        ),
    )
    p.add_argument("--opponent",    required=True,
                   help="Name of the opposing team (used in output filenames and directory)")
    p.add_argument("--input-dir",   default=DEFAULT_INPUT_DIR,
                   help=f"Directory containing source .mp4/.mov files (default: {DEFAULT_INPUT_DIR})")
    p.add_argument("--output-dir",  default=DEFAULT_OUTPUT_DIR,
                   help=f"Base directory for output; a sub-folder named <opponent>-<date> is created (default: {DEFAULT_OUTPUT_DIR})")
    p.add_argument("--lut",         default=DEFAULT_LUT,
                   help=f"Path to a .cube LUT file applied via lut3d filter (default: {DEFAULT_LUT})")
    p.add_argument("--target-fps",  type=int, default=DEFAULT_TARGET_FPS,
                   help=f"Frame rate for the slow-motion export pass (default: {DEFAULT_TARGET_FPS})")
    return p


# ---------------------------------------------------------------------------
# Helpers — ffprobe / metadata
# ---------------------------------------------------------------------------
def get_meta_value(file_path: str, show_entries: str) -> str:
    """Return the first value for the given ffprobe show_entries spec, or ''."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", show_entries,
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path),
    ]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode().strip()
        return out.splitlines()[0] if out else ""
    except Exception:
        return ""


def get_video_info(file_path: str) -> Optional[dict]:
    """Return duration, fps, width, height for the first video stream, or None."""
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=avg_frame_rate,duration,width,height",
        "-of", "json",
        str(file_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        if not data.get("streams"):
            return None
    except Exception:
        return None

    stream = data["streams"][0]
    duration = float(stream.get("duration", 0))
    fps_str = stream.get("avg_frame_rate", "0/1")
    if "/" in fps_str:
        num, den = map(int, fps_str.split("/"))
        fps = num / den if den != 0 else 0.0
    else:
        fps = float(fps_str)

    return {
        "duration": duration,
        "fps": fps,
        "width": stream.get("width"),
        "height": stream.get("height"),
    }


def get_creation_date(file_path: str) -> str:
    """Return YYYY-MM-DD from the clip's creation_time metadata, or today."""
    from datetime import date
    raw = get_meta_value(file_path, "stream_tags=creation_time")
    if raw:
        date_part = raw.split("T")[0]
        if re.match(r"\d{4}-\d{2}-\d{2}", date_part):
            return date_part
    return date.today().isoformat()


# ---------------------------------------------------------------------------
# Helpers — exposure analysis
# ---------------------------------------------------------------------------
def get_exposure_gain_filter(file_path: str, duration: float) -> tuple[str, float]:
    """
    Sample the clip at 25 %, 50 %, and 75 % of its duration, measuring YAVG
    directly from the **raw Canon Log3 source** (pre-LUT).

    YAVG is compared against LOG3_MIDGREY_10BIT (363 in 10-bit).  The ratio
    gives the linear gain needed to bring the average exposure to middle grey.

    The gain is applied via a ``lut`` filter that multiplies each Y (luma)
    value by the gain factor and clamps to the legal range.  This operates
    directly on the encoded pixel values with no colour-space conversion —
    the correct approach for log-encoded footage.  The ``exposure`` ffmpeg
    filter must NOT be used here because it linearises the signal first.

    Returns ``(ffmpeg_filter_str, stops)`` where ffmpeg_filter_str is ready
    to prepend to the filter chain, or ``('', 0.0)`` if no adjustment needed.
    """
    if duration < 1:
        return "", 0.0

    # Detect source bit depth once
    bits_str = get_meta_value(file_path, "stream=bits_per_raw_sample")
    try:
        bits = int(bits_str)
        if bits <= 8:
            raise ValueError
    except (ValueError, TypeError):
        pix_fmt = get_meta_value(file_path, "stream=pix_fmt")
        if "10" in pix_fmt:
            bits = 10
        elif "12" in pix_fmt:
            bits = 12
        else:
            bits = 8

    maxval = (2 ** bits) - 1          # 1023 for 10-bit, 255 for 8-bit
    # signalstats reports in native bit depth, so no normalisation needed
    # when comparing against a target scaled to the same bit depth.
    target = LOG3_MIDGREY_10BIT * (maxval / 1023)   # scale target to native bits

    samples = [duration * pct for pct in (0.25, 0.50, 0.75)]
    yavg_values = []

    for seek in samples:
        cmd = [
            "ffmpeg", "-ss", str(seek),
            "-i", str(file_path),
            "-frames:v", "1",
            "-vf", "signalstats,metadata=print",
            "-f", "null", "-",
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            combined = out.stdout + out.stderr
            for line in combined.splitlines():
                if "lavfi.signalstats.YAVG" in line:
                    try:
                        yavg_values.append(float(line.split("=")[-1].strip()))
                    except ValueError:
                        pass
                    break
        except Exception:
            continue

    if not yavg_values:
        return "", 0.0

    avg_yavg = sum(yavg_values) / len(yavg_values)

    if avg_yavg <= 0:
        return "", 0.0

    # gain: ratio of target to measured average in log-encoded space.
    # Multiplying encoded values by this constant shifts exposure uniformly.
    gain = target / avg_yavg
    stops = math.log2(gain)
    stops = max(-EXPOSURE_MAX_ADJ, min(EXPOSURE_MAX_ADJ, stops))

    if abs(stops) < EXPOSURE_MIN_ADJ:
        return "", 0.0

    # Re-derive gain from clamped stops for consistency
    gain = round(2 ** stops, 6)

    # lut filter: multiply Y by gain, clamp to [0, maxval].
    # 'val' in lut expressions is the raw pixel value in native bit depth.
    # Cb/Cr (chroma) are left unchanged — Log3 chroma is nearly neutral so
    # luma-only scaling is a good approximation of a true exposure change.
    lut_expr = f"lut=y='clip(val*{gain},0,{maxval})'"
    return lut_expr, round(stops, 4)


# ---------------------------------------------------------------------------
# Helpers — thumbnail generation (inline from generate_thumbnails.py)
# ---------------------------------------------------------------------------
def generate_card_thumbnail(video_path: str, output_path: str) -> None:
    """Generate a 480 px wide card thumbnail at t=1 s from the converted video."""
    cmd = [
        "ffmpeg", "-v", "error",
        "-i", str(video_path),
        "-ss", "00:00:01",
        "-vframes", "1",
        "-vf", "scale=480:-1",
        "-y", str(output_path),
    ]
    subprocess.run(cmd, check=True)


def generate_scrubber_thumbnails(
    video_path: str,
    interval: int = SPRITE_INTERVAL,
    width: int = SPRITE_WIDTH,
    columns: int = SPRITE_COLUMNS,
) -> None:
    """
    Generate a sprite sheet JPEG and a WebVTT file for video scrubber previews.
    Output files are written alongside the video file.
    """
    video_path = Path(video_path)
    basename = video_path.stem
    out_dir = video_path.parent

    sprite_path = out_dir / f"{basename}_sprite.jpg"
    vtt_path    = out_dir / f"{basename}_thumbnails.vtt"

    info = get_video_info(str(video_path))
    if not info:
        print(f"  Warning: could not read video info for scrubber thumbnails: {video_path.name}")
        return

    duration   = info["duration"]
    orig_w     = info["width"]
    orig_h     = info["height"]
    thumb_h    = int((width / orig_w) * orig_h)
    num_thumbs = math.ceil(duration / interval)
    rows       = math.ceil(num_thumbs / columns)

    print(f"  Generating scrubber sprite ({columns}×{rows} grid, {interval}s intervals) → {sprite_path.name}")

    sprite_cmd = [
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-filter_complex", f"fps=1/{interval},scale={width}:{thumb_h},tile={columns}x{rows}",
        "-frames:v", "1",
        "-q:v", "3",
        str(sprite_path),
    ]
    subprocess.run(sprite_cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def fmt_time(seconds: float) -> str:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = seconds % 60
        return f"{h:02d}:{m:02d}:{s:06.3f}"

    with open(vtt_path, "w") as f:
        f.write("WEBVTT\n\n")
        for i in range(num_thumbs):
            start = i * interval
            end   = min((i + 1) * interval, duration)
            col   = i % columns
            row   = i // columns
            x     = col * width
            y     = row * thumb_h
            f.write(f"{fmt_time(start)} --> {fmt_time(end)}\n")
            f.write(f"{basename}_sprite.jpg#xywh={x},{y},{width},{thumb_h}\n\n")

    print(f"  Scrubber assets written: {sprite_path.name}, {vtt_path.name}")


# ---------------------------------------------------------------------------
# Helpers — manifest generation (inline from generate_manifest.py)
# ---------------------------------------------------------------------------
def generate_manifest(output_dir: Path) -> None:
    """
    Scan output_dir for converted MP4 files, group them by clip, and write
    manifest.json into output_dir.  Existing tags in any prior manifest.json
    are preserved.  Does NOT write sports.json or folders.json.
    """
    pattern = re.compile(r"(.+)_(\d{4}-\d{2}-\d{2})_(\d+)_(\d+)fps\.mp4")

    # Preserve existing tags
    existing_tags: dict[str, list] = {}
    manifest_file = output_dir / "manifest.json"
    if manifest_file.exists():
        try:
            with open(manifest_file) as f:
                old = json.load(f)
            for v in old.get("videos", []):
                if "tags" in v:
                    key = f"{v['opponent']}_{v['date']}_{v['clip_num']}"
                    existing_tags[key] = v["tags"]
        except Exception as e:
            print(f"  Warning: could not read existing manifest for tags: {e}")

    video_groups: dict[str, dict] = {}

    for mp4 in sorted(output_dir.glob("*.mp4")):
        m = pattern.match(mp4.name)
        if not m:
            continue
        opponent_slug, date, clip_num, fps_label = m.groups()
        base_name = f"{opponent_slug}_{date}_{clip_num}"

        if base_name not in video_groups:
            video_groups[base_name] = {
                "opponent":  opponent_slug,
                "date":      date,
                "clip_num":  clip_num,
                "versions":  {},
                "thumbnail": f"{base_name}_thumb.jpg",
            }

        info = get_video_info(str(mp4))
        if info:
            capture_fps = int(fps_label)
            playback_fps = info["fps"]
            video_groups[base_name]["versions"][f"{fps_label}fps"] = {
                "filename":      mp4.name,
                "capture_fps":   capture_fps,
                "fps":           playback_fps,
                "duration":      info["duration"],
                "stretch_factor": capture_fps / playback_fps if playback_fps > 0 else 1.0,
            }

    videos = []
    for base_name, data in video_groups.items():
        if not data["versions"]:
            continue
        if base_name in existing_tags:
            data["tags"] = existing_tags[base_name]
        videos.append(data)

    videos.sort(key=lambda v: v["clip_num"])

    manifest = {
        "name":   output_dir.name,
        "videos": videos,
    }

    with open(manifest_file, "w") as f:
        json.dump(manifest, f, indent=4)

    print(f"Manifest written → {manifest_file}  ({len(videos)} clip(s))")


# ---------------------------------------------------------------------------
# Core — convert one clip (runs in a thread)
# ---------------------------------------------------------------------------
def convert_clip(
    video_path: Path,
    clip_num: str,
    video_date: str,
    opponent_slug: str,
    output_dir: Path,
    lut: str,
    target_fps: int,
    input_fps: float,
    height: int,
    unsharp: str = "",
) -> None:
    input_fps_int = round(input_fps)
    base_name     = f"{opponent_slug}_{video_date}_{clip_num}"
    primary_name  = f"{base_name}_{input_fps_int}fps.mp4"
    primary_path  = output_dir / primary_name

    # ---- Exposure analysis ------------------------------------------------
    print(f"[{clip_num}] Analyzing exposure for {primary_name}...")
    duration_str = get_meta_value(str(video_path), "format=duration")
    try:
        duration = float(duration_str)
    except (ValueError, TypeError):
        duration = 0.0

    # Measure YAVG on the raw Log3 source and build a lut filter that scales
    # encoded pixel values by the required gain before the 3D LUT.
    gain_filter, exposure_stops = get_exposure_gain_filter(str(video_path), duration)

    if gain_filter:
        print(f"[{clip_num}]   Applying exposure adjustment: {exposure_stops:+.4f} stops")
        filter_chain = f"{gain_filter},lut3d={lut}:interp=trilinear"
    else:
        print(f"[{clip_num}]   No exposure adjustment needed")
        filter_chain = f"lut3d={lut}:interp=trilinear"

    if unsharp:
        filter_chain = f"{filter_chain},{unsharp}"

    # ---- Pass 1: primary conversion ---------------------------------------
    print(f"[{clip_num}] Pass 1 (Primary): {primary_name} ({height}p)")
    cmd_primary = [
        "nice", "-n", "10",
        "ffmpeg", "-v", "error",
        "-i", str(video_path),
        "-vf", filter_chain,
        "-c:v", "h264_videotoolbox",
        "-b:v", "20M",
        "-an",
        "-movflags", "+faststart",
        "-y", str(primary_path),
    ]
    subprocess.run(cmd_primary, check=True)

    # ---- Card thumbnail ---------------------------------------------------
    thumb_path = output_dir / f"{base_name}_thumb.jpg"
    print(f"[{clip_num}]   Generating card thumbnail → {thumb_path.name}")
    try:
        generate_card_thumbnail(str(primary_path), str(thumb_path))
    except Exception as e:
        print(f"[{clip_num}]   Warning: card thumbnail failed: {e}")

    # ---- Scrubber thumbnails for primary ----------------------------------
    try:
        generate_scrubber_thumbnails(str(primary_path))
    except Exception as e:
        print(f"[{clip_num}]   Warning: scrubber thumbnails failed for {primary_name}: {e}")

    # ---- Pass 2: slow-motion (only when input_fps > target_fps) -----------
    if input_fps > target_fps:
        slow_name = f"{base_name}_{target_fps}fps.mp4"
        slow_path = output_dir / slow_name
        print(f"[{clip_num}] Pass 2 (Slow-Mo): {slow_name}")

        # Select 1 of every N frames (use 120 as the high-speed base)
        drop_frames = 120 // target_fps
        slow_filter = (
            f"select='not(mod(n,{drop_frames}))',"
            f"setpts=N/{target_fps}/TB,"
            f"{filter_chain}"
        )

        cmd_slow = [
            "nice", "-n", "10",
            "ffmpeg", "-v", "error",
            "-i", str(video_path),
            "-vf", slow_filter,
            "-r", str(target_fps),
            "-c:v", "h264_videotoolbox",
            "-b:v", "20M",
            "-an",
            "-movflags", "+faststart",
            "-y", str(slow_path),
        ]
        subprocess.run(cmd_slow, check=True)

        try:
            generate_scrubber_thumbnails(str(slow_path))
        except Exception as e:
            print(f"[{clip_num}]   Warning: scrubber thumbnails failed for {slow_name}: {e}")

    print(f"[{clip_num}] Finished.")


# ---------------------------------------------------------------------------
# Slug helper
# ---------------------------------------------------------------------------
def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_]", "", name.lower().replace(" ", "_"))
    return slug if slug else name.replace(" ", "_")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    args = build_parser().parse_args()

    opponent      = args.opponent
    input_dir     = Path(args.input_dir).resolve()
    base_out_dir  = Path(args.output_dir).resolve()
    lut           = args.lut
    target_fps    = args.target_fps
    unsharp       = ""  # e.g. "unsharp=3:3:0.25:3:3:0.1"

    opponent_slug = slugify(opponent)

    # Resolve LUT path relative to script location if not absolute
    script_dir = Path(__file__).resolve().parent
    if not Path(lut).is_absolute():
        lut_path = script_dir / lut
        if lut_path.exists():
            lut = str(lut_path)

    # Collect source files
    extensions = {".mp4", ".MP4", ".mov", ".MOV"}
    files = sorted(
        f for f in input_dir.iterdir()
        if f.is_file() and f.suffix in extensions
    )

    if not files:
        print(f"No video files found in {input_dir}")
        sys.exit(0)

    # Determine output date from first file's metadata
    video_date_dir = get_creation_date(str(files[0]))

    output_dir = base_out_dir / f"{opponent_slug}-{video_date_dir}"
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Found {len(files)} video(s). Opponent: {opponent} (slug: {opponent_slug})")
    print(f"Output directory: {output_dir}")

    # Build work items — metadata is read synchronously to preserve clip ordering
    work_items = []
    clip_index = 1

    for video_path in files:
        duration_str = get_meta_value(str(video_path), "format=duration")
        try:
            duration = float(duration_str)
        except (ValueError, TypeError):
            duration = 0.0

        if duration < 5:
            print(f"Skipping {video_path.name} (too short or unreadable: {duration:.1f}s)")
            continue

        video_date = get_creation_date(str(video_path))

        width_str  = get_meta_value(str(video_path), "stream=width")
        height_str = get_meta_value(str(video_path), "stream=height")
        try:
            width  = int(width_str)
            height = int(height_str)
        except (ValueError, TypeError):
            width, height = 0, 0

        if width == 1920:
            # 1080p is assumed to be 120 fps high-speed
            input_fps = 120.0
        else:
            fps_frac = get_meta_value(str(video_path), "stream=r_frame_rate")
            try:
                if "/" in fps_frac:
                    num, den = map(int, fps_frac.split("/"))
                    input_fps = num / den if den else 0.0
                else:
                    input_fps = float(fps_frac)
            except (ValueError, TypeError):
                input_fps = 0.0

        clip_num = f"{clip_index:03d}"
        clip_index += 1

        work_items.append({
            "video_path":    video_path,
            "clip_num":      clip_num,
            "video_date":    video_date,
            "opponent_slug": opponent_slug,
            "output_dir":    output_dir,
            "lut":           lut,
            "target_fps":    target_fps,
            "input_fps":     input_fps,
            "height":        height,
            "unsharp":       unsharp,
        })

    if not work_items:
        print("No eligible clips to process.")
        sys.exit(0)

    # Process clips in parallel
    errors = []
    with ThreadPoolExecutor(max_workers=MAX_JOBS) as pool:
        futures = {pool.submit(convert_clip, **item): item["clip_num"] for item in work_items}
        for future in as_completed(futures):
            clip_num = futures[future]
            try:
                future.result()
            except Exception as exc:
                print(f"[{clip_num}] ERROR: {exc}")
                errors.append((clip_num, exc))

    print(f"\nConversion complete. {len(work_items)} clip(s) processed, {len(errors)} error(s).")

    # Generate manifest.json for the output game folder
    print("\nGenerating manifest.json...")
    try:
        generate_manifest(output_dir)
    except Exception as e:
        print(f"Warning: manifest generation failed: {e}")


if __name__ == "__main__":
    main()
