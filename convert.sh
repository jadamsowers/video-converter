#!/bin/bash

# Parse named arguments
opponent=''
input_dir='/Volumes/EOS_DIGITAL/DCIM/100EOSR7'
base_output_dir='/Volumes/Data/Videos/Lacrosse'
lut='CinemaGamut_CanonLog3-to-Canon709_33_Ver.1.0.cube'
target_fps=60

while [[ $# -gt 0 ]]; do
    case "$1" in
        --opponent)   opponent="$2";        shift 2 ;;
        --input-dir)  input_dir="$2";       shift 2 ;;
        --output-dir) base_output_dir="$2"; shift 2 ;;
        --lut)        lut="$2";             shift 2 ;;
        --target-fps) target_fps="$2";      shift 2 ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

if [ -z "$opponent" ]; then
    echo "Error: --opponent is required."
    echo "Usage: $0 --opponent <name> [--input-dir <path>] [--output-dir <path>] [--lut <file>] [--target-fps <fps>]"
    exit 1
fi

# Configuration
unsharp='' # e.g., 'unsharp=3:3:0.25:3:3:0.1'

# Helper to extract metadata
get_meta() {
    # Usage: get_meta [file] [entry]
    ffprobe -v error -select_streams v:0 -show_entries "$2" -of default=noprint_wrappers=1:nokey=1 "$1" | head -n 1
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

output_dir="$base_output_dir/$opponent-$video_date_dir"
mkdir -p "$output_dir"

# Speed optimization: M1 Max parallel processing
MAX_JOBS=8  # Processing 8 streams at once given your dual hardware encoders

echo "Found $total_files videos. Opponent: $opponent. Output: $output_dir"

for video_path in "${files[@]}"; do
    
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
        base_name="${opponent}_${video_date}_${clip_num}"
        
        # PASS 1: Always convert (Primary version)
        primary_name="${base_name}_${input_fps_int}fps.mp4"
        echo "Processing Pass 1 (Primary): $primary_name (${height}p)"
        
        filter_chain="lut3d=$lut:interp=trilinear"
        [ -n "$unsharp" ] && filter_chain="$filter_chain,$unsharp"
        
        ffmpeg -v error -i "$video_path" \
            -vf "$filter_chain" \
            -c:v h264_videotoolbox \
            -b:v 20M \
            -an \
            -movflags +faststart \
            -y "$output_dir/$primary_name"
            
        # PASS 2: Special case for frame rate reduction (e.g. 120 -> 60)
        # Never add frames: only run if input_fps > target_fps
        if (( $(echo "$input_fps > $target_fps" | bc -l) )); then
            slow_name="${base_name}_${target_fps}fps.mp4"
            echo "Processing Pass 2 (Slow-Mo): $slow_name"
            
            # Select 1 of every N frames (120/target_fps = ratio)
            # Use 120 as standard high speed base
            drop_frames=$(echo "120 / $target_fps" | bc)
            slow_filter="select='not(mod(n,$drop_frames))',setpts=N/$target_fps/TB,$filter_chain"
            
            ffmpeg -v error -i "$video_path" \
                -vf "$slow_filter" \
                -r "$target_fps" \
                -c:v h264_videotoolbox \
                -b:v 20M \
                -an \
                -movflags +faststart \
                -y "$output_dir/$slow_name"
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