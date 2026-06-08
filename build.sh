#!/usr/bin/env bash
set -e

echo "=== FFmpeg install kar raha hai ==="
apt-get update -qq && apt-get install -y ffmpeg nodejs npm -qq

echo "=== Python packages install ==="
pip install -r requirements.txt

echo "=== yt-dlp latest update ==="
pip install yt-dlp --upgrade

echo "=== Build complete! ==="
