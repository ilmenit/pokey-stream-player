"""Tests for the stream-player package."""

import os
import sys
import struct
import wave
import tempfile
import unittest
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'src'))

from stream_player.asm6502 import Asm6502
from stream_player.tables import (quantize_single, quantize_dual, pack_dual_byte,
                                   VOLTAGE_TABLE_SINGLE, VOLTAGE_TABLE_DUAL)
from stream_player.audio import (load_audio, resample, encode_mono_dual,
                                  encode_stereo_dual, find_best_divisor)
from stream_player.compress import compress_bank, decompress_bank, compress_banks
from stream_player.layout import split_into_banks, bank_portb_table, BANK_SIZE
from stream_player.player_code import build_raw_player, build_lzsa_player
from stream_player.xex import XEXBuilder, build_xex
from stream_player.errors import *


# ═══════════════════════════════════════════════════════════════════════
# Assembler Tests
# ═══════════════════════════════════════════════════════════════════════

class TestAsm6502(unittest.TestCase):

    def test_lda_imm(self):
        a = Asm6502(0x1000)
        a.lda_imm(0x42)
        self.assertEqual(a.assemble(), bytes([0xA9, 0x42]))

    def test_sta_abs(self):
        a = Asm6502(0x1000)
        a.sta_abs(0xD201)
        self.assertEqual(a.assemble(), bytes([0x8D, 0x01, 0xD2]))

    def test_label_and_branch(self):
        a = Asm6502(0x1000)
        a.label('loop')
        a.dex()
        a.bne('loop')
        code = a.assemble()
        # DEX (1 byte) + BNE (2 bytes) → branch back 2 bytes
        self.assertEqual(code, bytes([0xCA, 0xD0, 0xFD]))

    def test_forward_branch(self):
        a = Asm6502(0x1000)
        a.beq('skip')
        a.lda_imm(0x42)
        a.label('skip')
        a.rts()
        code = a.assemble()
        # BEQ +2, LDA #$42, RTS
        self.assertEqual(code, bytes([0xF0, 0x02, 0xA9, 0x42, 0x60]))

    def test_jmp_label(self):
        a = Asm6502(0x2000)
        a.jmp('target')
        a.fill(10, 0xEA)
        a.label('target')
        a.rts()
        code = a.assemble()
        self.assertEqual(code[0], 0x4C)
        target_addr = code[1] | (code[2] << 8)
        self.assertEqual(target_addr, 0x2000 + 3 + 10)

    def test_jsr_label(self):
        a = Asm6502(0x2000)
        a.jsr('sub')
        a.rts()
        a.label('sub')
        a.lda_imm(0x99)
        a.rts()
        code = a.assemble()
        self.assertEqual(code[0], 0x20)

    def test_undefined_label_raises(self):
        a = Asm6502(0x1000)
        a.jmp('nowhere')
        with self.assertRaises(ValueError):
            a.assemble()

    def test_word_label(self):
        a = Asm6502(0x2000)
        a.label('here')
        a.byte(0x70, 0x70, 0x41)
        a.word('here')
        code = a.assemble()
        # word should be $2000 (little endian)
        self.assertEqual(code[3], 0x00)
        self.assertEqual(code[4], 0x20)

    def test_pc_tracking(self):
        a = Asm6502(0x3000)
        self.assertEqual(a.pc, 0x3000)
        a.lda_imm(0x00)  # 2 bytes
        self.assertEqual(a.pc, 0x3002)
        a.sta_abs(0xD201)  # 3 bytes
        self.assertEqual(a.pc, 0x3005)


# ═══════════════════════════════════════════════════════════════════════
# POKEY Table Tests
# ═══════════════════════════════════════════════════════════════════════

class TestPokeyTables(unittest.TestCase):

    def test_single_table_size(self):
        self.assertEqual(len(VOLTAGE_TABLE_SINGLE), 16)

    def test_dual_table_size(self):
        self.assertEqual(len(VOLTAGE_TABLE_DUAL), 31)

    def test_tables_monotonic(self):
        for i in range(1, len(VOLTAGE_TABLE_SINGLE)):
            self.assertGreater(VOLTAGE_TABLE_SINGLE[i], VOLTAGE_TABLE_SINGLE[i-1])
        for i in range(1, len(VOLTAGE_TABLE_DUAL)):
            self.assertGreater(VOLTAGE_TABLE_DUAL[i], VOLTAGE_TABLE_DUAL[i-1])

    def test_quantize_silence(self):
        audio = np.zeros(100, dtype=np.float32) - 1.0  # minimum amplitude
        idx = quantize_single(audio, noise_shaping=False)
        self.assertTrue(np.all(idx == 0))

    def test_quantize_max(self):
        audio = np.ones(100, dtype=np.float32)
        idx = quantize_single(audio, noise_shaping=False)
        self.assertTrue(np.all(idx == 15))

    def test_dual_pack(self):
        # Index 0 → (0, 0) → $00
        self.assertEqual(pack_dual_byte(0), 0x00)
        # Index 1 → (0, 1) → $01
        self.assertEqual(pack_dual_byte(1), 0x01)
        # Index 30 → (15, 15) → $FF
        self.assertEqual(pack_dual_byte(30), 0xFF)


# ═══════════════════════════════════════════════════════════════════════
# Audio Tests
# ═══════════════════════════════════════════════════════════════════════

class TestAudio(unittest.TestCase):

    def _make_wav(self, path, sr=44100, duration=0.5, freq=440, channels=1):
        """Generate a test WAV file."""
        t = np.linspace(0, duration, int(sr * duration), dtype=np.float32)
        mono = (np.sin(2 * np.pi * freq * t) * 0.8).astype(np.float32)
        samples = (mono * 32767).astype(np.int16)
        if channels == 2:
            samples = np.column_stack([samples, samples]).flatten()
        with wave.open(path, 'wb') as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(samples.tobytes())

    def test_load_wav_mono(self):
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            path = f.name
        try:
            self._make_wav(path, sr=22050, channels=1)
            audio, sr, ch = load_audio(path)
            self.assertEqual(sr, 22050)
            self.assertEqual(ch, 1)
            self.assertEqual(audio.ndim, 1)
            self.assertGreater(len(audio), 0)
        finally:
            os.unlink(path)

    def test_load_wav_stereo(self):
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as f:
            path = f.name
        try:
            self._make_wav(path, sr=44100, channels=2)
            audio, sr, ch = load_audio(path)
            self.assertEqual(ch, 2)
            self.assertEqual(audio.ndim, 2)
            self.assertEqual(audio.shape[1], 2)
        finally:
            os.unlink(path)

    def test_resample(self):
        audio = np.sin(np.linspace(0, 10, 1000, dtype=np.float32))
        out = resample(audio, 44100, 15000)
        expected_len = int(1000 * 15000 / 44100)
        self.assertAlmostEqual(len(out), expected_len, delta=2)

    def test_find_divisor(self):
        div, rate, audctl = find_best_divisor(15000)
        self.assertGreater(rate, 10000)
        self.assertLess(rate, 20000)
        self.assertIn(audctl, [0x00, 0x40])

    def test_encode_mono(self):
        audio = np.sin(np.linspace(0, 10, 1000, dtype=np.float32)) * 0.5
        encoded = encode_mono_dual(audio)
        self.assertEqual(len(encoded), 1000)
        # All bytes should be valid dual-channel values
        for b in encoded:
            v1 = (b >> 4) & 0x0F
            v2 = b & 0x0F
            self.assertLessEqual(v1, 15)
            self.assertLessEqual(v2, 15)

    def test_encode_stereo(self):
        audio = np.column_stack([
            np.sin(np.linspace(0, 10, 500, dtype=np.float32)) * 0.5,
            np.cos(np.linspace(0, 10, 500, dtype=np.float32)) * 0.5,
        ])
        encoded = encode_stereo_dual(audio)
        self.assertEqual(len(encoded), 1000)  # 2 bytes per sample


# ═══════════════════════════════════════════════════════════════════════
# Compression Tests
# ═══════════════════════════════════════════════════════════════════════

class TestCompression(unittest.TestCase):

    def test_roundtrip_silence(self):
        """Silence = all zeros → should compress well."""
        indices = bytes([15] * 500)  # constant mid-level = silence
        comp, _ = compress_bank(indices, 0)
        dec = decompress_bank(comp)
        self.assertEqual(dec, indices)
        self.assertLess(len(comp), len(indices) // 2)

    def test_roundtrip_ramp(self):
        """Linear ramp → constant deltas → excellent compression."""
        indices = bytes(list(range(31)) * 10)
        comp, _ = compress_bank(indices, 0)
        dec = decompress_bank(comp)
        self.assertEqual(dec, indices)

    def test_roundtrip_random(self):
        """Random indices → still decompresses correctly."""
        np.random.seed(42)
        indices = bytes(np.random.randint(0, 31, 4096, dtype=np.uint8))
        comp, _ = compress_bank(indices, 0)
        dec = decompress_bank(comp)
        self.assertEqual(dec, indices)

    def test_roundtrip_audio_like(self):
        """Simulated audio: smooth with noise."""
        np.random.seed(42)
        t = np.linspace(0, 10, 4096, dtype=np.float32)
        sig = np.sin(t) * 12 + 15 + np.random.randn(4096) * 0.5
        indices = bytes(np.clip(sig, 0, 30).astype(np.uint8))
        comp, _ = compress_bank(indices, 0)
        dec = decompress_bank(comp)
        self.assertEqual(dec, indices)
        # Should compress significantly (smooth signal)
        self.assertLess(len(comp), len(indices) * 0.6)

    def test_bank_continuity(self):
        """Multi-bank delta continuity: prev_value propagates."""
        indices = bytes([10, 11, 12, 13, 14, 15])
        # First bank ends at index 15
        b1, _ = compress_bank(indices[:3], prev_value=0)
        d1 = decompress_bank(b1)
        self.assertEqual(d1, indices[:3])
        
        # Second bank starts from where first left off
        b2, _ = compress_bank(indices[3:], prev_value=indices[2])
        d2 = decompress_bank(b2)
        self.assertEqual(d2, indices[3:])

    def test_compress_banks(self):
        """compress_banks splits and compresses, roundtrip correct."""
        # Force multiple banks by using small bank_size with enough data
        np.random.seed(42)
        indices = bytes(np.random.randint(0, 31, 50000, dtype=np.uint8))
        banks, pos = compress_banks(indices, bank_size=2048, max_banks=64)
        self.assertGreaterEqual(len(banks), 1)
        self.assertEqual(pos, len(indices))
        # Each bank must fit in the bank_size
        for b in banks:
            self.assertLessEqual(len(b), 2048)
        # Verify round-trip
        result = bytearray()
        for bank_data in banks:
            result.extend(decompress_bank(bank_data))
        self.assertEqual(bytes(result), indices)


# ═══════════════════════════════════════════════════════════════════════
# Bank Layout Tests
# ═══════════════════════════════════════════════════════════════════════

class TestLayout(unittest.TestCase):

    def test_single_bank(self):
        data = bytes(range(256))
        banks = split_into_banks(data)
        self.assertEqual(len(banks), 1)
        self.assertEqual(banks[0], data)

    def test_multi_bank(self):
        data = bytes([0xFF]) * (BANK_SIZE * 3 + 100)
        banks = split_into_banks(data)
        self.assertEqual(len(banks), 4)
        self.assertEqual(len(banks[0]), BANK_SIZE)
        self.assertEqual(len(banks[-1]), 100)

    def test_overflow(self):
        data = bytes([0x00]) * (BANK_SIZE * 65)
        with self.assertRaises(BankOverflowError):
            split_into_banks(data, max_banks=64)

    def test_portb_table(self):
        portb = bank_portb_table(4)
        self.assertEqual(len(portb), 4)
        # First bank PORTB should be $E3
        self.assertEqual(portb[0], 0xE3)


# ═══════════════════════════════════════════════════════════════════════
# Player Code Tests
# ═══════════════════════════════════════════════════════════════════════

class TestPlayerCode(unittest.TestCase):

    def test_build_raw_mono(self):
        code, origin, start = build_raw_player(
            pokey_divisor=0x37, audctl=0x40, n_banks=4,
            portb_table=[0xE3, 0xC3, 0xA3, 0x83],
            stereo=False)
        self.assertIsInstance(code, bytes)
        self.assertGreater(len(code), 100)
        self.assertEqual(origin, 0x2000)
        self.assertGreater(start, origin)

    def test_build_raw_stereo(self):
        code, origin, start = build_raw_player(
            pokey_divisor=0x37, audctl=0x40, n_banks=2,
            portb_table=[0xE3, 0xC3],
            stereo=True)
        self.assertGreater(len(code), 100)

    def test_build_lzsa_mono(self):
        code, origin, start = build_lzsa_player(
            pokey_divisor=0x37, audctl=0x40, n_banks=4,
            portb_table=[0xE3, 0xC3, 0xA3, 0x83],
            stereo=False)
        self.assertGreater(len(code), 200)

    def test_raw_player_contains_portb(self):
        """Verify PORTB table is embedded in player code."""
        portb = [0xE3, 0xC3, 0xA3]
        code, _, _ = build_raw_player(0x37, 0x40, 3, portb, False)
        # PORTB values should appear in the code
        for p in portb:
            self.assertIn(bytes([p]), code)

    def test_irq_vector_written(self):
        """Verify IRQ vector stores target an address within the code."""
        code, origin, start = build_raw_player(0x37, 0x40, 1, [0xE3], False)
        # Find STA $FFFE (8D FE FF) and check LDA #imm before it
        found = False
        for i in range(len(code) - 2):
            if code[i] == 0x8D and code[i+1] == 0xFE and code[i+2] == 0xFF:
                if i >= 2 and code[i-2] == 0xA9:
                    irq_lo = code[i-1]
                    found = True
                    break
        self.assertTrue(found, "IRQ vector store not found in code")
        # The IRQ handler address should be within the code range
        # Find the high byte store too
        for i in range(len(code) - 2):
            if code[i] == 0x8D and code[i+1] == 0xFF and code[i+2] == 0xFF:
                if i >= 2 and code[i-2] == 0xA9:
                    irq_hi = code[i-1]
                    irq_addr = irq_lo | (irq_hi << 8)
                    self.assertGreaterEqual(irq_addr, origin)
                    self.assertLess(irq_addr, origin + len(code))
                    break

    def test_raw_bank_in_bank_out(self):
        """Verify IRQ has bank-in/read/bank-out pattern (critical!)."""
        code, origin, _ = build_raw_player(0x37, 0x40, 4,
            [0xE3, 0xC3, 0xA3, 0x83], False)
        # Pattern: LDA portb_table,X / STA PORTB / ... read ... / LDA #$FE / STA PORTB
        portb_tbl = origin + 1  # after NMI handler RTI
        bank_in = None
        bank_out = None
        sample_read = None
        for i in range(len(code) - 5):
            # LDA abs,X = BD
            if code[i] == 0xBD:
                target = code[i+1] | (code[i+2] << 8)
                if target == portb_tbl:
                    bank_in = i
            # LDA (zp),Y = B1 80 (sample_ptr)
            if code[i] == 0xB1 and code[i+1] == 0x80 and bank_in is not None and sample_read is None:
                sample_read = i
            # LDA #$FE / STA $D301
            if (code[i] == 0xA9 and code[i+1] == 0xFE and
                i+4 < len(code) and
                code[i+2] == 0x8D and code[i+3] == 0x01 and code[i+4] == 0xD3):
                if sample_read is not None:
                    bank_out = i
                    break
        self.assertIsNotNone(bank_in, "Bank-in not found")
        self.assertIsNotNone(sample_read, "Sample read not found")
        self.assertIsNotNone(bank_out, "Bank-out not found")
        self.assertLess(bank_in, sample_read)
        self.assertLess(sample_read, bank_out)


# ═══════════════════════════════════════════════════════════════════════
# XEX Builder Tests
# ═══════════════════════════════════════════════════════════════════════

class TestXEX(unittest.TestCase):

    def test_simple_xex(self):
        xex = XEXBuilder()
        xex.add_segment(0x2000, bytes([0xA9, 0x42, 0x60]))
        xex.set_run_address(0x2000)
        data = xex.build()
        # Check header
        self.assertEqual(data[0], 0xFF)
        self.assertEqual(data[1], 0xFF)
        # Check segment start
        self.assertEqual(data[2], 0x00)  # $2000 low
        self.assertEqual(data[3], 0x20)  # $2000 high

    def test_multi_segment(self):
        xex = XEXBuilder()
        xex.add_segment(0x2000, bytes([0x60]))
        xex.add_segment(0x3000, bytes([0x60]))
        data = xex.build()
        self.assertGreater(len(data), 10)

    def test_bank_data(self):
        xex = XEXBuilder()
        xex.add_segment(0x2000, bytes([0x60]))  # player stub
        xex.add_bank_data(0, bytes([0xAA] * 100))
        xex.set_run_address(0x2000)
        data = xex.build()
        self.assertIn(bytes([0xAA] * 10), data)

    def test_build_xex_function(self):
        player = bytes([0xA9, 0x42, 0x60])
        banks = [bytes([0x11] * 100), bytes([0x22] * 100)]
        xex_data = build_xex(player, 0x2000, banks, 0x2000)
        self.assertIsInstance(xex_data, bytes)
        self.assertGreater(len(xex_data), 200)
        # Should start with $FF $FF header
        self.assertEqual(xex_data[0], 0xFF)
        self.assertEqual(xex_data[1], 0xFF)


# ═══════════════════════════════════════════════════════════════════════
# End-to-End Test
# ═══════════════════════════════════════════════════════════════════════

class TestEndToEnd(unittest.TestCase):

    def _make_test_wav(self, path, duration=1.0, sr=44100):
        t = np.linspace(0, duration, int(sr * duration), dtype=np.float32)
        samples = (np.sin(2 * np.pi * 440 * t) * 0.5 * 32767).astype(np.int16)
        with wave.open(path, 'wb') as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(samples.tobytes())

    def test_full_pipeline_raw(self):
        """Test complete WAV → XEX pipeline in RAW mode."""
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wf:
            wav_path = wf.name
        xex_path = wav_path.replace('.wav', '.xex')
        
        try:
            self._make_test_wav(wav_path, duration=0.5)
            
            from stream_player.cli import main
            result = main([wav_path, '-r', '8000', '-o', xex_path])
            
            self.assertEqual(result, 0)
            self.assertTrue(os.path.exists(xex_path))
            
            with open(xex_path, 'rb') as f:
                xex_data = f.read()
            self.assertGreater(len(xex_data), 1000)
            self.assertEqual(xex_data[0], 0xFF)
            self.assertEqual(xex_data[1], 0xFF)
        finally:
            for p in [wav_path, xex_path]:
                if os.path.exists(p):
                    os.unlink(p)

    def test_full_pipeline_compressed(self):
        """Test complete WAV → XEX pipeline with compression."""
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wf:
            wav_path = wf.name
        xex_path = wav_path.replace('.wav', '.xex')
        
        try:
            self._make_test_wav(wav_path, duration=0.5)
            
            from stream_player.cli import main
            result = main([wav_path, '-r', '8000', '--compression', 'lz', '-o', xex_path])
            
            self.assertEqual(result, 0)
            self.assertTrue(os.path.exists(xex_path))
        finally:
            for p in [wav_path, xex_path]:
                if os.path.exists(p):
                    os.unlink(p)

    def test_full_pipeline_vq(self):
        """Test complete WAV → XEX pipeline with VQ compression."""
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wf:
            wav_path = wf.name
        xex_path = wav_path.replace('.wav', '.xex')

        try:
            self._make_test_wav(wav_path, duration=0.5)

            from stream_player.cli import main
            for vs in ['4', '8', '16']:
                result = main([wav_path, '--compression', 'vq',
                               '--vec-size', vs, '-o', xex_path])
                self.assertEqual(result, 0, f"VQ vec_size={vs} failed")
                self.assertTrue(os.path.exists(xex_path))
                os.unlink(xex_path)
        finally:
            for p in [wav_path, xex_path]:
                if os.path.exists(p):
                    os.unlink(p)


class TestVQ(unittest.TestCase):
    """VQ encoder/decoder tests."""

    def test_roundtrip(self):
        """VQ encode then decode recovers approximate data."""
        from stream_player.vq import vq_encode_banks, vq_decode_banks
        rng = np.random.RandomState(42)
        # Smooth signal: sine quantized to 0-30
        t = np.linspace(0, 2*np.pi*10, 2000)
        indices = np.clip(np.round(15 + 14*np.sin(t)), 0, 30).astype(np.uint8)

        for vs in [4, 8, 16]:
            banks, n_enc = vq_encode_banks(indices, vec_size=vs,
                                           max_level=30, n_iter=10)
            decoded = vq_decode_banks(banks, vs, total_samples=n_enc)
            rmse = np.sqrt(np.mean((indices[:n_enc].astype(float)
                                    - decoded.astype(float))**2))
            self.assertLess(rmse, 2.0, f"VQ vec={vs} RMSE too high: {rmse}")

    def test_bank_geometry(self):
        """Bank geometry calculations are correct."""
        from stream_player.vq import vq_bank_geometry
        for vs, expected_cb in [(4, 1024), (8, 2048), (16, 4096)]:
            cb, ipb, spb = vq_bank_geometry(vs)
            self.assertEqual(cb, expected_cb)
            self.assertEqual(ipb, 16384 - expected_cb)
            self.assertEqual(spb, ipb * vs)

    def test_vq_player_builds(self):
        """VQ player builds for all channel/vec_size combinations."""
        from stream_player.player_code import build_vq_player
        from stream_player.layout import bank_portb_table
        portb = bank_portb_table(4)
        for vs in [4, 8, 16]:
            for nch in [1, 2, 3, 4]:
                code, orig, start = build_vq_player(
                    0xDD, 0x40, 4, portb, False,
                    pokey_channels=nch, vec_size=vs)
                self.assertGreater(len(code), 500)
                self.assertEqual(orig, 0x2000)

    def test_accepts_bytes_input(self):
        """VQ encoder accepts bytes as well as numpy arrays."""
        from stream_player.vq import vq_encode_banks
        data = bytes(range(32)) * 4  # 128 bytes
        banks, n_enc = vq_encode_banks(data, vec_size=8,
                                       max_level=31, n_iter=5)
        self.assertGreater(n_enc, 0)
        self.assertGreater(len(banks), 0)


class TestEnhance(unittest.TestCase):
    """Audio enhancement tests."""

    def test_compress_dynamics(self):
        """Dynamic compression reduces peak-to-quiet ratio."""
        from stream_player.enhance import compress_dynamics
        # Signal with big dynamic range
        loud = np.ones(100, dtype=np.float32) * 0.9
        quiet = np.ones(100, dtype=np.float32) * 0.05
        audio = np.concatenate([loud, quiet])

        compressed = compress_dynamics(audio, strength=0.5)
        # Quiet parts should be boosted relative to loud parts
        ratio_before = np.mean(np.abs(audio[:100])) / np.mean(np.abs(audio[100:]))
        ratio_after = np.mean(np.abs(compressed[:100])) / np.mean(np.abs(compressed[100:]))
        self.assertLess(ratio_after, ratio_before)

    def test_zoh_preemphasis(self):
        """ZOH pre-emphasis boosts high frequencies."""
        from stream_player.enhance import apply_zoh_preemphasis
        sr = 8000
        t = np.linspace(0, 0.1, int(sr * 0.1))
        # 3 kHz tone should be boosted
        audio = np.sin(2 * np.pi * 3000 * t).astype(np.float32)
        boosted = apply_zoh_preemphasis(audio, sr)
        # RMS should increase (treble boost)
        self.assertGreater(np.sqrt(np.mean(boosted**2)),
                           np.sqrt(np.mean(audio**2)))

    def test_enhance_bypass_at_zero(self):
        """Strength 0 is a no-op."""
        from stream_player.enhance import compress_dynamics
        audio = np.random.randn(500).astype(np.float32) * 0.5
        result = compress_dynamics(audio, strength=0.0)
        np.testing.assert_array_equal(audio, result)

    def test_full_pipeline_enhanced(self):
        """Full pipeline works with --enhance."""
        with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as wf:
            wav_path = wf.name
        xex_path = wav_path.replace('.wav', '.xex')
        try:
            self._make_test_wav(wav_path)
            from stream_player.cli import main
            result = main([wav_path, '-e', '-o', xex_path])
            self.assertEqual(result, 0)
            self.assertTrue(os.path.exists(xex_path))
        finally:
            for p in [wav_path, xex_path]:
                if os.path.exists(p):
                    os.unlink(p)

    def _make_test_wav(self, path, duration=0.3, sr=8000):
        t = np.linspace(0, duration, int(sr * duration))
        audio = (np.sin(2 * np.pi * 440 * t) * 0.5 * 32767).astype(np.int16)
        with open(path, 'wb') as f:
            data = audio.tobytes()
            f.write(b'RIFF')
            f.write(struct.pack('<I', 36 + len(data)))
            f.write(b'WAVEfmt ')
            f.write(struct.pack('<IHHIIHH', 16, 1, 1, sr, sr * 2, 2, 16))
            f.write(b'data')
            f.write(struct.pack('<I', len(data)))
            f.write(data)


if __name__ == '__main__':
    unittest.main(verbosity=2)
