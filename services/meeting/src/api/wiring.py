"""Adapters bridging ``SessionRegistry``'s injected ``deps`` protocol to the
real Opus/AWS/LLM modules, plus the process-wide ``SessionRegistry`` FastAPI
dependency.

``src/sessions.py`` is deliberately decoupled from AWS/network so it
unit-tests with fakes. This module is the only place that wires it to the real
world: real per-speaker Opus decoders, a real AWS Transcribe client factory,
and a real LLM-backed report builder (minutes + PDF).
"""

from datetime import datetime, timezone
from functools import lru_cache

from src.audio.decoder import OpusStreamDecoder
from src.config import get_settings
from src.pipeline.llm_client import LlmClient
from src.pipeline.minutes import summarize_minutes
from src.pipeline.pdf import render_meeting_pdf
from src.pipeline.transcript import assemble_transcript
from src.sessions import SessionRegistry
from src.stt.transcribe import create_transcription_stream


class AudioAdapter:
    """Factory for per-speaker, stateful Opus decoders.

    Discord voice delivers bare Opus packets (no container/demuxer framing),
    and Opus decode carries state across packets -- so decoding can't be a
    single stateless ``decode(frame)`` call shared across speakers (that was
    the live-integration bug: ffmpeg has no demuxer for standalone Opus
    packets, and failed with exit 234 against real Discord audio). Instead,
    each speaker gets its OWN ``OpusStreamDecoder`` instance (see
    ``make_decoder``), created once in ``_SpeakerBuffer.__init__`` and fed
    that speaker's packets in order for the life of the session.
    """

    def make_decoder(self) -> OpusStreamDecoder:
        return OpusStreamDecoder()


def _build_report_builder():
    settings = get_settings()
    # LlmClient puts the key straight into an X-API-Key header, so unwrap here:
    # the SecretStr boundary stops at this edge, not inside the client.
    llm_client = LlmClient(
        settings.llm_base_url,
        settings.llm_api_key.get_secret_value(),
        settings.request_timeout_s,
    )

    def report_builder(segments, meta):
        transcript = assemble_transcript(segments)
        minutes = summarize_minutes(transcript, llm_client)
        pdf_bytes = render_meeting_pdf(minutes, transcript, meta)
        return minutes, pdf_bytes

    return report_builder


@lru_cache(maxsize=1)
def get_session_registry() -> SessionRegistry:
    """FastAPI dependency: process-wide ``SessionRegistry`` wired to real
    Opus/AWS/LLM adapters. Tests override via
    ``app.dependency_overrides[get_session_registry] = lambda: fake_registry``."""
    deps = {
        "make_transcription_stream": lambda: create_transcription_stream(get_settings().aws_region),
        "audio": AudioAdapter(),
        "report_builder": _build_report_builder(),
        "now": lambda: datetime.now(timezone.utc),
        "max_meeting_ms": get_settings().max_meeting_ms,
    }
    return SessionRegistry(deps)
