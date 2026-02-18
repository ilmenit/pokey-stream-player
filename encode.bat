@echo off
rem Encode audio to Atari 8-bit XEX (development launcher)
rem Usage: encode <input-file> [options]
cd /d "%~dp0"
set PYTHONPATH=src
python -m stream_player %*
