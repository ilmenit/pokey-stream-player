"""Error types for stream-player."""


class StreamPlayerError(Exception):
    """Base error for stream-player."""
    pass


class AudioLoadError(StreamPlayerError):
    """Failed to load or decode audio file."""
    pass


class EncodingError(StreamPlayerError):
    """Failed to encode audio to POKEY format."""
    pass


class CompressionError(StreamPlayerError):
    """LZSA compression failed."""
    pass


class BankOverflowError(StreamPlayerError):
    """Audio data exceeds available bank memory."""
    pass


class XEXBuildError(StreamPlayerError):
    """Failed to build XEX binary."""
    pass
