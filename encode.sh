#!/usr/bin/env bash
# Encode audio to Atari 8-bit XEX (development launcher)
# Usage: ./encode.sh <input-file> [options]
cd "$(dirname "$0")"
PYTHONPATH=src python3 -m stream_player "$@"
