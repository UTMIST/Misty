"""Decode bare Discord Opus packets into 16 kHz mono s16le PCM.

Discord voice delivers ONE bare Opus packet per ~20ms WS frame (48 kHz,
2 channels, 960 samples/channel) -- there is no Ogg/container framing around
it. ffmpeg has no demuxer that can make sense of a standalone Opus packet fed
in one at a time (there's no elementary-stream reader for "raw Opus"; ffmpeg's
Opus support expects either an Ogg container or a full RTP/WebM stream with
header packets). Feeding bare packets through ``ffmpeg -f data -c:a libopus``
fails live with "Stream #0:0: Data: none" / "Output file does not contain any
stream" (exit 234) -- confirmed against real Discord audio.

The fix is a real, stateful Opus decoder (via PyAV, which bundles its own
ffmpeg/libopus so it installs cleanly in ``python:3.11-slim`` with no extra
system package): one decoder instance per speaker, fed packets in order.
"""

import av

# Output format is s16 (16-bit) mono, so one sample is 2 bytes.
_BYTES_PER_SAMPLE = 2


class OpusStreamDecoder:
    """Stateful per-speaker Opus decoder: bare 48 kHz/2ch Opus packets in,
    16 kHz mono s16le PCM out.

    Opus decoding carries state across packets (packet-loss concealment,
    internal history), so each speaker MUST get its own instance and packets
    for that speaker MUST be fed to it in order -- never share one instance
    across speakers or interleave packets from different speakers into it.
    """

    def __init__(self, sample_rate: int = 48000, channels: int = 2, out_rate: int = 16000) -> None:
        self._codec = av.CodecContext.create("libopus", "r")
        self._codec.sample_rate = sample_rate
        self._codec.layout = "stereo" if channels == 2 else "mono"
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=out_rate)

    def decode(self, opus_packet: bytes) -> bytes:
        """Decode one bare Opus packet, returning 16 kHz mono s16le PCM bytes.

        Returns ``b""`` if the packet yields no audio (e.g. an empty/DTX
        packet) or fails to decode (a single malformed packet must not take
        down the whole speaker's stream -- decode is best-effort per packet).
        """
        if not opus_packet:
            return b""
        try:
            frames = self._codec.decode(av.Packet(opus_packet))
        except av.error.FFmpegError:
            return b""

        pcm = bytearray()
        for frame in frames:
            resampled = self._resampler.resample(frame)
            for out_frame in resampled if isinstance(resampled, list) else [resampled]:
                if out_frame is None:
                    continue
                # Slice to the REAL sample count. ``bytes(plane)`` hands back
                # the whole plane allocation, which FFmpeg aligns -- for a 320
                # sample (20ms) frame that's 768 bytes instead of 640, i.e. 20%
                # trailing padding. Splicing that in would both corrupt the
                # audio sent to Transcribe (a ~6ms glitch every frame) and
                # inflate the byte-derived timebase that ``sessions.py`` uses to
                # detect silence gaps. s16 mono => 2 bytes per sample.
                pcm += bytes(out_frame.planes[0])[: out_frame.samples * _BYTES_PER_SAMPLE]
        return bytes(pcm)
