"""Stream Player CLI — Convert audio to Atari XEX with dual POKEY.

Usage:
    python -m stream_player input.wav -r 15000 -o output.xex
    python -m stream_player song.mp3 -r 8000 -c lz -o output.xex
"""

import argparse
import os
import sys
import time

from .errors import (StreamPlayerError, AudioLoadError, EncodingError,
                     CompressionError, BankOverflowError, XEXBuildError)
from .audio import (load_audio, resample, encode_audio, encode_indices,
                    find_best_divisor, PAL_CLOCK)
from .compress import compress_banks, decompress_bank
from .layout import split_into_banks, bank_portb_table, format_bank_info, MAX_BANKS
from .tables import max_level
from .player_code import build_raw_player, build_lzsa_player
from .xex import build_xex


def _fmt_duration(seconds: float) -> str:
    """Format seconds as m:ss or h:mm:ss."""
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def _progress_bar(done: int, total: int, width: int = 30) -> str:
    """Render a simple progress bar string."""
    frac = done / total if total > 0 else 1.0
    filled = int(width * frac)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {frac:5.1%}"


def _compress_progress(pos, total, n_banks):
    """Print compression progress on one line."""
    bar = _progress_bar(pos, total)
    print(f"\r  {bar}  {pos:,}/{total:,} samples, {n_banks} banks", end="", flush=True)


def _print_usage():
    """Print friendly usage when invoked with no arguments."""
    print("""
  ╔═══════════════════════════════════════════════════════════════╗
  ║  stream-player — Audio to Atari 8-bit XEX converter         ║
  ║  1-4 channel POKEY PCM from extended memory (XL/XE)          ║
  ╚═══════════════════════════════════════════════════════════════╝

  USAGE:
    encode <input-file> [options]

  QUICK START:
    encode song.mp3                            Default: 2ch, LZ compressed
    encode song.mp3 -e                         Treble pre-emphasis for HW
    encode song.mp3 -c vq                      VQ (7× compression, lossy)
    encode song.mp3 -c vq -e                   VQ + enhanced
    encode song.mp3 -n 4                       4ch (louder, slight roughness)
    encode song.mp3 -o my_song.xex             Custom output name

  COMMON OPTIONS:
    -c off|lz|vq          Compression mode (default: lz)
    -s 4|8|16             VQ vector size (default: 8)
    -n 1|2|3|4            POKEY channels (default: 2)
    -e, --enhance         Treble pre-emphasis for real HW (default: off)
    -r RATE               Sample rate in Hz (default: 8000)
    -o FILE               Output file (default: <input>.xex)
    -h, --help            Full help with all options

  CHANNELS → QUALITY vs CPU:
    1 ch: 16 levels, ~80cy/IRQ  │  3 ch: 46 levels, ~96cy/IRQ
    2 ch: 31 levels, ~88cy/IRQ  │  4 ch: 61 levels, ~104cy/IRQ

  SUPPORTED FORMATS:
    WAV, MP3, FLAC, OGG, AIFF (via soundfile, no external binaries)
    MOD, XM, S3M, IT, SID, ... (require ffmpeg installed)

  MEMORY & DURATION (8 kHz, mono, 2ch):
    Memory              Raw     DeltaLZ    VQ (vec=8)
    130XE (64KB)        ~8s      ~10s       ~57s
    256KB               ~32s     ~42s       ~3:48
    512KB               1:05     ~1:25      ~7:36
    1MB                 2:11     ~2:50      ~15:12
""")


def main(argv=None):
    """Main entry point."""
    if argv is None and len(sys.argv) < 2:
        _print_usage()
        return 0

    parser = argparse.ArgumentParser(
        prog='encode',
        description='Convert audio files to Atari 8-bit XEX with POKEY playback.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  encode song.mp3                                   Default: 2ch, DeltaLZ
  encode song.mp3 --compression vq                     VQ compression (~7x, lossy)
  encode song.mp3 --compression vq -s 16       VQ with larger vectors (~12x)
  encode song.mp3 --compression off                    Raw (no compression)
  encode song.mp3 --channels 4                      4ch (louder, slight roughness)

compression modes:
  lz    DeltaLZ — lossless, ~1.3x on music
  vq    Vector Quantization — lossy (~-3dB), ~7x with vec=8
  off   No compression (1 byte per sample)

duration (8 kHz mono 2ch, 1MB: raw ~2:11 / DeltaLZ ~2:50 / VQ ~15:12)""")

    parser.add_argument('input',
                        help='Input audio file (WAV, MP3, FLAC, OGG, MOD, ...)')
    parser.add_argument('-o', '--output', default=None,
                        help='Output .xex file or directory for --asm (default: <input>.xex)')
    parser.add_argument('-r', '--rate', type=int, default=8000,
                        help='Sample rate in Hz (default: 8000). Lower = longer duration')
    parser.add_argument('--mode', choices=['1cps', 'scalar'], default='scalar',
                        help='Encoding: scalar (default) or 1cps (1 write/IRQ, 12+ kHz)')
    parser.add_argument('-n', '--channels', type=int, choices=[1, 2, 3, 4], default=2,
                        help='POKEY channels (1-4, default 2). More = louder but rougher')
    parser.add_argument('--asm', action='store_true',
                        help='Output MADS assembly project instead of XEX')
    parser.add_argument('-c', '--compression', choices=['off', 'lz', 'vq'], default='lz',
                        help='Compression: off, lz (DeltaLZ, default), vq (Vector Quantization)')
    parser.add_argument('-s', '--vec-size', type=int, choices=[4, 8, 16], default=8,
                        help='VQ vector size (default: 8). Only used with -c vq')
    parser.add_argument('-e', '--enhance', action='store_true',
                        help='Treble pre-emphasis to compensate POKEY DAC rolloff')
    parser.add_argument('--max-banks', type=int, default=MAX_BANKS,
                        help=f'Max extended memory banks (default: {MAX_BANKS})')
    parser.add_argument('--no-noise-shaping', action='store_true',
                        help='Disable noise shaping (slightly faster, lower quality)')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Show compression verification details')

    args = parser.parse_args(argv)
    # compression stays as string: 'off', 'lz', 'vq'

    try:
        return run(args)
    except StreamPlayerError as e:
        print(f"\nError: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"\nUnexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        if args.verbose:
            import traceback
            traceback.print_exc()
        return 2


def run(args) -> int:
    """Execute the conversion pipeline."""
    t0 = time.time()

    # ── Derive output path ──
    if args.output:
        output_path = args.output
    elif args.asm:
        base = os.path.splitext(args.input)[0]
        output_path = base + '_asm'
    else:
        base = os.path.splitext(args.input)[0]
        output_path = base + '.xex'

    # ── 1. Load audio ──
    print(f"Loading: {args.input}")
    audio, src_rate, n_channels = load_audio(args.input)

    n_samples = audio.shape[0]
    input_duration = n_samples / src_rate
    ch_str = f"{n_channels} channel{'s' if n_channels > 1 else ''}"
    print(f"  Format: {src_rate} Hz, {ch_str}")
    print(f"  Duration: {_fmt_duration(input_duration)} ({n_samples:,} samples)")

    if input_duration < 0.1:
        raise AudioLoadError("Audio too short (< 0.1 seconds)")

    # ── 2. Find POKEY divisor ──
    divisor, actual_rate, audctl = find_best_divisor(args.rate)
    clk_name = "1.77MHz" if (audctl & 0x40) else "64kHz"
    print(f"\nPOKEY timer:")
    print(f"  Requested: {args.rate} Hz → divisor ${divisor:02X}, AUDCTL=${audctl:02X} ({clk_name})")
    print(f"  Actual: {actual_rate:.1f} Hz")

    # ── 3. Resample ──
    if abs(src_rate - actual_rate) / actual_rate > 0.001:
        print(f"\nResampling {src_rate} Hz → {actual_rate:.0f} Hz...")
        audio_rs = resample(audio, src_rate, int(actual_rate))
        print(f"  Output: {audio_rs.shape[0]:,} samples")
    else:
        audio_rs = audio
        print(f"\n  Sample rate matches, no resampling needed.")

    # ── 4. Encode to POKEY format ──
    ch_mode = "mono"
    noise_shaping = not args.no_noise_shaping
    bytes_per_sec = actual_rate
    truncated = False

    if args.compression == 'vq':
        # ── VQ mode ──
        from .vq import vq_encode_banks, vq_bank_geometry
        vs = args.vec_size
        cb_b, ipb, spb = vq_bank_geometry(vs)

        ns_label = 'noise-shaped' if noise_shaping else 'nearest'
        if args.enhance:
            ns_label += '+enhanced'
        print(f"\nEncoding ({ch_mode}, {args.channels}-channel, {ns_label})...")
        indices = encode_indices(audio_rs, n_channels, False, noise_shaping,
                                sample_rate=int(actual_rate),
                                pokey_channels=args.channels, mode='scalar',
                                enhance=args.enhance)
        print(f"  {len(indices):,} samples at {bytes_per_sec:,.0f} samples/sec")

        print(f"\nVQ encoding (vec_size={vs}, 256 codes per bank)...")

        def vq_progress(done, total, n_banks):
            pct = done / total if total else 1
            bar_w = 30
            filled = int(bar_w * pct)
            bar = '█' * filled + '░' * (bar_w - filled)
            print(f"\r  [{bar}] {pct*100:.1f}%  {done:,}/{total:,} samples, "
                  f"{n_banks} banks", end='', flush=True)

        vq_banks, samples_compressed = vq_encode_banks(
            indices, vec_size=vs, max_banks=args.max_banks,
            max_level=max_level(args.channels), n_iter=20,
            progress_fn=vq_progress)
        print()

        encoded_duration = samples_compressed / bytes_per_sec
        compression = samples_compressed / (len(vq_banks) * 16384) if vq_banks else 1

        if samples_compressed < len(indices):
            truncated = True
            lost = len(indices) - samples_compressed
            lost_sec = lost / bytes_per_sec
            print(f"  Filled {len(vq_banks)} banks ({args.max_banks} max), "
                  f"encoded {_fmt_duration(encoded_duration)} "
                  f"of {_fmt_duration(input_duration)}")
            print(f"  Truncated {_fmt_duration(lost_sec)} "
                  f"({lost:,} samples) to fit available memory.")
        else:
            print(f"  {len(vq_banks)} banks, "
                  f"{compression:.1f}× compression (vec_size={vs})")

        banks = vq_banks
        portb = bank_portb_table(len(banks))

    elif args.compression == 'lz':
        # ── DeltaLZ mode ──
        enc_mode = args.mode
        mode_label = f"1CPS" if enc_mode == '1cps' else f"{args.channels}-channel"
        ns_label = 'noise-shaped' if noise_shaping else 'nearest'
        if args.enhance:
            ns_label += '+enhanced'
        print(f"\nEncoding ({ch_mode}, {mode_label}, {ns_label})...")
        indices = encode_indices(audio_rs, n_channels, False, noise_shaping,
                                sample_rate=int(actual_rate), pokey_channels=args.channels,
                                mode=enc_mode, enhance=args.enhance)
        print(f"  {len(indices):,} samples at {bytes_per_sec:,.0f} samples/sec")

        use_delta = (enc_mode != '1cps')
        lz_label = 'DeltaLZ' if use_delta else 'RawLZ'
        print(f"\nCompressing ({lz_label})...")
        compressed_banks, samples_compressed = compress_banks(
            indices, bank_size=16384, max_banks=args.max_banks,
            progress_fn=_compress_progress, use_delta=use_delta)
        print()  # newline after progress bar

        comp_size = sum(len(b) for b in compressed_banks)
        ratio = comp_size / samples_compressed if samples_compressed > 0 else 1.0
        encoded_duration = samples_compressed / bytes_per_sec

        if samples_compressed < len(indices):
            truncated = True
            lost = len(indices) - samples_compressed
            lost_sec = lost / bytes_per_sec
            print(f"  Filled {len(compressed_banks)} banks "
                  f"({args.max_banks} max), encoded {_fmt_duration(encoded_duration)} "
                  f"of {_fmt_duration(input_duration)}")
            print(f"  Truncated {_fmt_duration(lost_sec)} "
                  f"({lost:,} samples) to fit available memory.")
        else:
            print(f"  {len(compressed_banks)} banks, "
                  f"{comp_size:,} bytes, ratio {ratio:.0%}")

        if args.verbose:
            print(f"  Verifying decompression...")
            result = bytearray()
            for bank_data in compressed_banks:
                result.extend(decompress_bank(bank_data, use_delta))
            expected = indices[:samples_compressed]
            if bytes(result) != bytes(expected):
                raise CompressionError(
                    f"Verification failed: expected {len(expected)}, "
                    f"got {len(result)}")
            print(f"  OK ({len(result):,} samples)")

        banks = compressed_banks
        portb = bank_portb_table(len(banks))

    else:
        # ── RAW mode ──
        ns_label = 'noise-shaped' if noise_shaping else 'nearest'
        if args.enhance:
            ns_label += '+enhanced'
        print(f"\nEncoding ({ch_mode}, {args.channels}-channel, {ns_label})...")
        encoded = encode_audio(audio_rs, n_channels, False, noise_shaping,
                               sample_rate=int(actual_rate),
                               pokey_channels=args.channels,
                               enhance=args.enhance)
        print(f"  {len(encoded):,} bytes ({len(encoded) // 1024}KB) "
              f"at {bytes_per_sec:,.0f} bytes/sec")

        max_raw = args.max_banks * 16384
        if len(encoded) > max_raw:
            truncated = True
            kept = max_raw
            encoded = encoded[:kept]
            encoded_duration = kept / bytes_per_sec
            print(f"\n  Truncated to {_fmt_duration(encoded_duration)} "
                  f"of {_fmt_duration(input_duration)} "
                  f"to fit {args.max_banks} banks ({max_raw // 1024}KB).")
        else:
            encoded_duration = len(encoded) / bytes_per_sec

        banks = split_into_banks(encoded, args.max_banks)
        portb = bank_portb_table(len(banks))
        print(f"\n  {len(banks)} banks, "
              f"{sum(len(b) for b in banks):,} bytes")

    # ── 5. Build output ──
    if args.compression == 'vq':
        mode_label = f'VQ{args.vec_size}'
    elif args.compression == 'lz':
        enc_mode = args.mode
        mode_label = '1CPS-DeltaLZ' if enc_mode == '1cps' else 'DeltaLZ'
    else:
        mode_label = 'RAW'

    if args.asm:
        if args.compression == 'vq':
            raise EncodingError("--asm output is not yet supported with VQ compression. "
                                "Use XEX output (default) instead.")
        # ── ASM output: generate MADS project ──
        from .asm_output import generate_asm_project
        print(f"\nGenerating MADS assembly project ({mode_label}, {len(banks)} banks)...")
        source_name = os.path.basename(args.input)
        asm_path = generate_asm_project(
            output_dir=output_path,
            banks=banks,
            portb_table=portb,
            divisor=divisor,
            audctl=audctl,
            stereo=False,
            compressed=(args.compression == 'lz'),
            actual_rate=actual_rate,
            duration=encoded_duration,
            source_name=source_name,
            pokey_channels=args.channels,
        )

        elapsed = time.time() - t0
        total_bin = sum(len(b) for b in banks)

        print(f"\n{'=' * 50}")
        print(f"  {output_path}/")
        print(f"  stream_player.asm + {len(banks)} bank files")
        print(f"  {total_bin // 1024} KB bank data, {mode_label} {ch_mode}")
        print(f"  {actual_rate:.0f} Hz (POKEY div ${divisor:02X}, AUDCTL=${audctl:02X})")
        if truncated:
            print(f"  Encoded: {_fmt_duration(encoded_duration)} "
                  f"of {_fmt_duration(input_duration)} (truncated to fit)")
        else:
            print(f"  Duration: {_fmt_duration(encoded_duration)}")
        print(f"  Generated in {elapsed:.1f}s")
        print(f"{'=' * 50}")
        print(f"\n  Assemble with: cd {output_path} && mads stream_player.asm -o:stream_player.xex")

    else:
        # ── XEX output: build binary directly ──
        if args.compression == 'vq':
            from .player_code import build_vq_player
            player_code, player_origin, start_addr = build_vq_player(
                divisor, audctl, len(banks), portb, False,
                pokey_channels=args.channels, vec_size=args.vec_size,
                sample_rate=actual_rate)
        elif args.compression == 'lz':
            player_code, player_origin, start_addr = build_lzsa_player(
                divisor, audctl, len(banks), portb, False,
                pokey_channels=args.channels, mode=enc_mode,
                sample_rate=actual_rate)
        else:
            player_code, player_origin, start_addr = build_raw_player(
                divisor, audctl, len(banks), portb, False,
                pokey_channels=args.channels,
                sample_rate=actual_rate)

        print(f"\nBuilding XEX ({mode_label}, {len(banks)} banks)...")
        from .player_code import build_charset_copy_init
        charset_init = build_charset_copy_init()
        xex_data = build_xex(player_code, player_origin, banks, start_addr,
                             charset_init=charset_init)

        with open(output_path, 'wb') as f:
            f.write(xex_data)

        elapsed = time.time() - t0
        xex_kb = len(xex_data) / 1024

        print(f"\n{'=' * 50}")
        print(f"  {os.path.basename(output_path)}")
        print(f"  {xex_kb:.1f} KB, {len(banks)} banks, {mode_label} {ch_mode}")
        print(f"  {actual_rate:.0f} Hz (POKEY div ${divisor:02X})")
        if truncated:
            print(f"  Encoded: {_fmt_duration(encoded_duration)} "
                  f"of {_fmt_duration(input_duration)} (truncated to fit)")
        else:
            print(f"  Duration: {_fmt_duration(encoded_duration)}")
        print(f"  Built in {elapsed:.1f}s")
        print(f"{'=' * 50}")

    if len(banks) >= 4:
        print(f"\n  Requires {len(banks)} extended memory banks "
              f"({len(banks) * 16}KB).")

    return 0
