"""AWS client configuration that keeps idle audio streams from monopolizing Python."""

import time

from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.serialize import TranscribeStreamingSerializer
from amazon_transcribe.structures import BufferableByteStream

_EMPTY_READ_BACKOFF_S = 0.001


class _YieldingAudioBuffer(BufferableByteStream):
    def read(self, size=-1):
        try:
            return super().read(size)
        except BlockingIOError:
            # CRT retries empty reads in a tight native loop. Those callbacks
            # contend for the GIL with Opus decode and the WebSocket reader;
            # its pong can then sit behind audio until the keepalive expires.
            # This runs on a CRT thread, never on the asyncio event loop. Bound
            # the wait because that CRT thread may also own other connections.
            time.sleep(_EMPTY_READ_BACKOFF_S)
            raise


class _StreamingSerializer(TranscribeStreamingSerializer):
    def serialize_start_stream_transcription_request(self, endpoint, request_shape):
        request = super().serialize_start_stream_transcription_request(endpoint, request_shape)
        request.body = _YieldingAudioBuffer()
        return request


def create_transcribe_client(region: str) -> TranscribeStreamingClient:
    client = TranscribeStreamingClient(region=region)
    # The SDK has no public input-buffer factory. Replace its serializer on
    # this client only, BEFORE signing and opening HTTP. HTTP and AudioStream
    # must share the same buffer; swapping it after connection loses audio.
    # The offline SDK integration test guards this private hook on upgrades.
    client._serializer = _StreamingSerializer()
    return client
