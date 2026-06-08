#!/usr/bin/env bash
set -e

echo "=== FFmpeg + Node.js install ==="
apt-get update -qq && apt-get install -y ffmpeg nodejs npm -qq

echo "=== Python packages install ==="
pip install -r requirements.txt

echo "=== yt-dlp-ejs install (YouTube JS runtime) ==="
pip install yt-dlp-ejs --upgrade

echo "=== Build complete! ==="
