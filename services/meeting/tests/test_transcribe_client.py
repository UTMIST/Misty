"""Exercise the real SDK and CRT buffer interface with an in-memory HTTP transport."""

import asyncio
import time
from functools import partial
from types import SimpleNamespace

import pytest
from amazon_transcribe.auth import StaticCredentialResolver
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.eventstream import EventStreamBuffer
from awscrt.io import InputStream

from src.stt import client as client_module
from src.stt.client import _YieldingAudioBuffer, create_transcribe_client
from src.stt.transcribe import create_transcription_stream


def test_crt_empty_read_polling_releases_cpu():
    reader = InputStream.wrap(_YieldingAudioBuffer())
    target = memoryview(bytearray(32))
    wall_start = time.monotonic()
    cpu_start = time.thread_time()

    # Use CRT's actual callback entry point, including its readinto1/read1
    # dispatch and conversion of BlockingIOError into a would-block result.
    for _ in range(200):
        assert reader._read_into_memoryview(target) is None

    wall_elapsed = time.monotonic() - wall_start
    cpu_elapsed = time.thread_time() - cpu_start
    assert wall_elapsed >= 0.1
    assert cpu_elapsed < wall_elapsed / 2


def test_buffer_preserves_partial_reads_order_and_eof():
    body = _YieldingAudioBuffer()
    body.write(b"abc")
    body.write(b"def")
    body.end_stream()
    reader = InputStream.wrap(body)
    target = memoryview(bytearray(2))
    chunks = []
    while size := reader._read_into_memoryview(target):
        chunks.append(bytes(target[:size]))

    assert b"".join(chunks) == b"abcdef"
    assert reader._read_into_memoryview(target) == 0
    with pytest.raises(OSError):
        body.write(b"too late")


def test_sdk_signing_audio_events_and_http_share_the_yielding_buffer(monkeypatch):
    # The real SDK signs requests/events, but credentials and HTTP are fake.
    monkeypatch.setattr(
        client_module,
        "TranscribeStreamingClient",
        partial(
            TranscribeStreamingClient,
            credential_resolver=StaticCredentialResolver("test-key", "test-secret"),
        ),
    )

    class FakeResponse:
        async def resolve_response(self):
            return SimpleNamespace(status_code=200, headers={})

    class FakeTransport:
        body = None

        async def make_request(self, url, *, method, headers, body):
            assert method == "POST"
            assert "authorization" in {name.lower() for name, _ in headers}
            self.body = body
            return FakeResponse()

    async def run():
        client = create_transcribe_client("us-east-1")
        transport = FakeTransport()
        client._session_manager = transport
        stream = await client.start_stream_transcription(
            language_code="en-US", media_sample_rate_hz=16000, media_encoding="pcm"
        )
        assert isinstance(transport.body, _YieldingAudioBuffer)
        assert stream.input_stream._input_stream is transport.body

        await stream.input_stream.send_audio_event(b"first PCM chunk")
        await stream.input_stream.send_audio_event(b"second PCM chunk")
        await stream.input_stream.end_stream()

        # Read through the same CRT callback used for HTTP, with deliberately
        # small reads spanning event boundaries; validate SDK CRCs and framing.
        reader = InputStream.wrap(transport.body)
        target = memoryview(bytearray(7))
        events = EventStreamBuffer()
        while size := reader._read_into_memoryview(target):
            events.add_data(bytes(target[:size]))
        signed = list(events)
        assert len(signed) == 3
        assert all(":chunk-signature" in event.headers for event in signed)
        assert signed[-1].payload == b""
        audio = []
        for event in signed[:-1]:
            nested = EventStreamBuffer()
            nested.add_data(event.payload)
            [message] = list(nested)
            assert message.headers[":event-type"] == "AudioEvent"
            audio.append(message.payload)
        assert audio == [b"first PCM chunk", b"second PCM chunk"]

    asyncio.run(run())


def test_transcription_resolves_and_reuses_the_configured_client(monkeypatch):
    created = []
    client = object()

    def factory(region):
        created.append(region)
        return client

    monkeypatch.setattr(client_module, "create_transcribe_client", factory)
    stream = create_transcription_stream("us-east-1")
    assert stream._resolve_client() is client
    assert stream._resolve_client() is client
    assert created == ["us-east-1"]
