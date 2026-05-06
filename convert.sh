#!/bin/bash

# Parse named arguments
opponent=''
input_dir='/Volumes/EOS_DIGITAL/DCIM/100EOSR7'
base_output_dir='/Volumes/Data/Videos/Lacrosse'
lut='CinemaGamut_CanonLog3-to-Canon709_33_Ver.1.0.cube'
target_fps=60
MAX_JOBS=4  # Processing 8 streams at once given your dual hardware encoders
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
THUMB_SCRIPT="$SCRIPT_DIR/../video-sharing/generate_thumbnails.py"

usage() {
    cat <<EOF
Usage: $(basename "$0") --opponent <name> [OPTIONS]

Convert Canon Log3 footage from a camera card to H.264 MP4 with LUT applied.
Clips are named automatically using the opponent name, recording date, and clip
number. High-speed 1080p clips (assumed 120 fps) are also exported at the
target frame rate as a slow-motion version.

REQUIRED:
  --opponent <name>       Name of the opposing team (used in output filenames
                          and directory, e.g. "TeamName")

OPTIONS:
  --input-dir  <path>     Directory containing source .mp4/.mov files
                          (default: /Volumes/EOS_DIGITAL/DCIM/100EOSR7)
  --output-dir <path>     Base directory for output; a sub-folder named
                          <opponent>-<date> is created automatically
                          (default: /Volumes/Data/Videos/Lacrosse)
  --lut        <file>     Path to a .cube LUT file applied via lut3d filter
                          (default: CinemaGamut_CanonLog3-to-Canon709_33_Ver.1.0.cube)
  --target-fps <fps>      Frame rate for the slow-motion export pass; only
                          runs when the source fps exceeds this value
                          (default: 60)
  --help                  Show this help message and exit

OUTPUT NAMING:
  Pass 1 (primary):   <opponent>_<YYYY-MM-DD>_<NNN>_<src_fps>fps.mp4
  Pass 2 (slow-mo):   <opponent>_<YYYY-MM-DD>_<NNN>_<target_fps>fps.mp4
    (Pass 2 only runs when source fps > --target-fps)

EXAMPLES:
  $(basename "$0") --opponent Loyola
  $(basename "$0") --opponent Loyola --target-fps 30
  $(basename "$0") --opponent Loyola --input-dir ~/Desktop/footage --output-dir ~/Movies
  $(basename "$0") --opponent Loyola --lut ~/luts/custom.cube --target-fps 30

DEPENDENCIES:
  ffmpeg / ffprobe   https://ffmpeg.org
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --opponent)   opponent="$2";        shift 2 ;;
        --input-dir)  input_dir="$2";       shift 2 ;;
        --output-dir) base_output_dir="$2"; shift 2 ;;
        --lut)        lut="$2";             shift 2 ;;
        --target-fps) target_fps="$2";      shift 2 ;;
        --help)       usage; exit 0 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

if [ -z "$opponent" ]; then
    echo "Error: --opponent is required."
    echo
    usage
    exit 1
fi

# Sanitize opponent name for filesystem use (remove spaces/special chars)
opponent_slug=$(echo "$opponent" | tr '[:upper:]' '[:lower:]' | tr ' ' '_' | tr -cd '[:alnum:]_')
# If slug is empty (all special chars), fallback to original with underscores
[ -z "$opponent_slug" ] && opponent_slug=$(echo "$opponent" | tr ' ' '_')

# Resolve absolute paths to prevent issues in background subshells
[[ "$input_dir" != /* ]] && input_dir="$PWD/$input_dir"
[[ "$base_output_dir" != /* ]] && base_output_dir="$PWD/$base_output_dir"

# Configuration
unsharp='' # e.g., 'unsharp=3:3:0.25:3:3:0.1'

# Helper to extract metadata
get_meta() {
    # Usage: get_meta [file] [entry]
    ffprobe -v error -select_streams v:0 -show_entries "$2" -of default=noprint_wrappers=1:nokey=1 "$1" | head -n 1
}

# Helper to analyze footage and calculate exposure adjustment (in stops)
get_exposure_adj() {
    local file="$1"
    local dur=$(get_meta "$file" "format=duration")
    
    # If duration is missing or very short, skip analysis
    if [[ -z "$dur" ]] || (( $(echo "$dur < 1" | bc -l) )); then
        echo "0"
        return
    fi

    # Sample at 25%, 50%, and 75% for a better average
    local p25=$(echo "$dur * 0.25" | bc)
    local p50=$(echo "$dur * 0.50" | bc)
    local p75=$(echo "$dur * 0.75" | bc)
    
    local sum=0
    local count=0
    for p in $p25 $p50 $p75; do
        # Extract average luminance (YAVG) using signalstats
        local val=$(ffmpeg -ss "$p" -i "$file" -frames:v 1 -vf signalstats,metadata=print -f null - 2>&1 | grep "lavfi.signalstats.YAVG" | cut -d= -f2 | head -n 1)
        if [[ -n "$val" ]]; then
            sum=$(echo "$sum + $val" | bc)
            ((count++))
        fi
    done
    
    if [ "$count" -eq 0 ]; then
        echo "0"
        return
    fi
    
    local avg_yavg=$(echo "scale=2; $sum / $count" | bc)
    
    # Detect bit depth to normalize YAVG (signalstats returns native depth values)
    local bits=$(get_meta "$file" "stream=bits_per_raw_sample")
    # If bits is not a number or <= 8, fall back to pix_fmt check
    if [[ ! "$bits" =~ ^[0-9]+$ ]] || [ "$bits" -le 8 ]; then
        local pix_fmt=$(get_meta "$file" "stream=pix_fmt")
        if [[ "$pix_fmt" == *10* ]]; then bits=10; 
        elif [[ "$pix_fmt" == *12* ]]; then bits=12; 
        else bits=8; fi
    fi
    
    # Normalize avg_yavg to an 8-bit scale (0-255)
    local divisor=$(echo "2^($bits - 8)" | bc)
    local norm_avg=$(echo "scale=2; $avg_yavg / $divisor" | bc)
    
    # Target middle grey (normalized to 8-bit). 
    # 105 is a good target for punchy sports footage.
    local target=105
    
    # Avoid division by zero or log of zero
    if (( $(echo "$norm_avg <= 0" | bc -l) )); then
        echo "0"
        return
    fi
    
    # Calculate exposure adjustment: log2(target / norm_avg)
    local adj=$(echo "scale=4; l($target / $norm_avg) / l(2)" | bc -l)
    
    # Clamp to reasonable range to avoid extreme distortion
    if (( $(echo "$adj > 2.0" | bc -l) )); then adj="2.0"; fi
    if (( $(echo "$adj < -2.0" | bc -l) )); then adj="-2.0"; fi
    
    # Return 0 if adjustment is negligible (e.g., < 0.15 stops)
    local abs_adj=$(echo "scale=4; if ($adj < 0) -($adj) else $adj" | bc -l)
    if (( $(echo "$abs_adj < 0.15" | bc -l) )); then
        echo "0"
        return
    fi
    
    echo "$adj"
}

shopt -s nullglob
files=("$input_dir"/*.{mp4,MP4,mov,MOV})
total_files=${#files[@]}
current_jobs=0
clip_index=1

# Dynamically determine the video date from the first file (assume all are same date)
if [ "$total_files" -gt 0 ]; then
    first_creation_time=$(get_meta "${files[0]}" "stream_tags=creation_time")
    video_date_dir=$(echo "$first_creation_time" | cut -d'T' -f1)
    [ -z "$video_date_dir" ] && video_date_dir=$(date +%Y-%m-%d)
else
    video_date_dir=$(date +%Y-%m-%d)
fi

# Date with NO dashes for the filename (YYYYMMDD)
today_clean=$(echo "$video_date_dir" | tr -d '-')

output_dir="$base_output_dir/$opponent_slug-$video_date_dir"
mkdir -p "$output_dir"

echo "Found $total_files videos. Opponent: $opponent (using $opponent_slug for paths). Output: $output_dir"

for video_path in "${files[@]}"; do
    
    # Skip short clips (less than 5 seconds)
    duration=$(get_meta "$video_path" "format=duration")
    if [[ -z "$duration" ]] || (( $(echo "$duration < 5" | bc -l) )); then
        echo "Skipping $(basename "$video_path") (too short or unreadable: ${duration:-0}s)"
        continue
    fi
    
    # Extract metadata synchronously to ensure correct clip numbering and naming
    creation_time=$(get_meta "$video_path" "stream_tags=creation_time")
    # Format: 2026-03-18T21:57:15.000000Z -> 2026-03-18 (keep dashes)
    video_date=$(echo "$creation_time" | cut -d'T' -f1)
    [ -z "$video_date" ] && video_date=$video_date_dir
    
    # Detect resolution and frame rate
    width=$(get_meta "$video_path" "stream=width")
    height=$(get_meta "$video_path" "stream=height")
    
    if [ "$width" -eq 1920 ]; then
        # Rule: 1080p is assumed to be 120fps high-speed
        input_fps=120
    else
        # Rule: 4K (3840) or others use detected rate
        input_fps_frac=$(get_meta "$video_path" "stream=r_frame_rate")
        input_fps=$(echo "$input_fps_frac" | bc -l)
    fi
    
    # Closest integer for naming
    input_fps_int=$(printf "%.0f" "$input_fps")

    
    # Determine clip number with leading zeros (e.g. 001)
    clip_num=$(printf "%03d" "$clip_index")
    ((clip_index++))

    # Background the conversion process
    (
        base_name="${opponent_slug}_${video_date}_${clip_num}"
        
        # PASS 1: Always convert (Primary version)
        primary_name="${base_name}_${input_fps_int}fps.mp4"
        echo "Processing Pass 1 (Primary): $primary_name (${height}p)"
        
        # Analyze exposure
        echo "Analyzing exposure for $primary_name..."
        exposure_result=$(get_exposure_adj "$video_path")
        
        if [ "$exposure_result" != "0" ]; then
            echo "  Applying exposure adjustment: $exposure_result stops"
            filter_chain="exposure=$exposure_result,lut3d=$lut:interp=trilinear"
        else
            echo "  No exposure adjustment needed (or negligible)"
            filter_chain="lut3d=$lut:interp=trilinear"
        fi
        
        [ -n "$unsharp" ] && filter_chain="$filter_chain,$unsharp"
        
        nice -n 10 ffmpeg -v error -i "$video_path" \
            -vf "$filter_chain" \
            -c:v h264_videotoolbox \
            -b:v 20M \
            -an \
            -movflags +faststart \
            -y "$output_dir/$primary_name"
            
        # Generate card thumbnail (once per clip)
        echo "  Generating card thumbnail for $base_name..."
        ffmpeg -v error -i "$output_dir/$primary_name" -ss 00:00:01 -vframes 1 -vf "scale=480:-1" -y "$output_dir/${base_name}_thumb.jpg"

        # Generate scrubber thumbnails for primary
        if [ -f "$THUMB_SCRIPT" ]; then
            echo "  Generating scrubber thumbnails for $primary_name..."
            python3 "$THUMB_SCRIPT" "$output_dir/$primary_name"
        fi
            
        # PASS 2: Special case for frame rate reduction (e.g. 120 -> 60)
        # Never add frames: only run if input_fps > target_fps
        if (( $(echo "$input_fps > $target_fps" | bc -l) )); then
            slow_name="${base_name}_${target_fps}fps.mp4"
            echo "Processing Pass 2 (Slow-Mo): $slow_name"
            
            # Select 1 of every N frames (120/target_fps = ratio)
            # Use 120 as standard high speed base
            drop_frames=$(echo "120 / $target_fps" | bc)
            slow_filter="select='not(mod(n,$drop_frames))',setpts=N/$target_fps/TB,$filter_chain"
            
            nice -n 10 ffmpeg -v error -i "$video_path" \
                -vf "$slow_filter" \
                -r "$target_fps" \
                -c:v h264_videotoolbox \
                -b:v 20M \
                -an \
                -movflags +faststart \
                -y "$output_dir/$slow_name"
                
            # Generate scrubber thumbnails for slow-mo
            if [ -f "$THUMB_SCRIPT" ]; then
                echo "  Generating scrubber thumbnails for $slow_name..."
                python3 "$THUMB_SCRIPT" "$output_dir/$slow_name"
            fi
        fi
        
        echo "Finished clip $clip_num"
    ) &

    # Manage parallel jobs (Bash 3.2 compatible)
    while [ $(jobs -rp | wc -l) -ge $MAX_JOBS ]; do
        sleep 0.5
    done
done

wait
echo "Conversion complete. All $total_files videos processed."