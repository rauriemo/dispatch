"""Tests for dispatch.stt -- frame conversion, debug fallback."""

import queue
import struct
from unittest.mock import AsyncMock, patch

from dispatch.stt import debug_transcribe


class TestFrameConversion:
    def test_frame_to_bytes_conversion(self):
        """int16 list -> struct.pack produces correct LINEAR16 bytes."""
        frame = [0, 1000, -1000, 32767, -32768]
        result = struct.pack(f"<{len(frame)}h", *frame)

        assert len(result) == 10  # 5 values * 2 bytes each
        # Verify individual values round-trip
        assert struct.unpack("<h", result[0:2])[0] == 0
        assert struct.unpack("<h", result[2:4])[0] == 1000
        assert struct.unpack("<h", result[4:6])[0] == -1000
        assert struct.unpack("<h", result[6:8])[0] == 32767
        assert struct.unpack("<h", result[8:10])[0] == -32768

    def test_frame_to_bytes_round_trip(self):
        """Pack frame to bytes, unpack back, verify it matches original."""
        original = [100, -200, 300, -400, 500, 0, -32768, 32767]
        packed = struct.pack(f"<{len(original)}h", *original)
        unpacked = list(struct.unpack(f"<{len(original)}h", packed))
        assert unpacked == original


class TestDebugTranscribe:
    async def test_debug_transcribe_returns_input(self):
        """debug_transcribe should return the typed input."""
        fq = queue.Queue()
        with patch(
            "dispatch.stt.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value="hello world",
        ):
            result = await debug_transcribe(fq)
        assert result == "hello world"

    async def test_debug_transcribe_strips_whitespace(self):
        """debug_transcribe should strip leading/trailing whitespace."""
        fq = queue.Queue()
        with patch(
            "dispatch.stt.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value="  hello world  ",
        ):
            result = await debug_transcribe(fq)
        assert result == "hello world"
