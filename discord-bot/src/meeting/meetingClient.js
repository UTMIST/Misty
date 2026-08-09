import WsWebSocket from 'ws';

export class MeetingUnavailable extends Error {
  constructor(message) {
    super(message);
    this.name = 'MeetingUnavailable';
  }
}

// Transport client for the internal meeting service: a WebSocket audio/control
// stream plus a small HTTP surface (transcript/stop). Pure transport — no
// discord.js here; the binary frame layout below MUST byte-match the
// service's parser (2-byte BE speaker_id length, speaker_id utf8, 8-byte BE
// ts_ms, remaining bytes = opus payload).
export function createMeetingClient({ baseUrl, wsUrl, apiKey, WebSocketImpl = WsWebSocket, fetchImpl = fetch }) {
  function encodeFrame(speakerId, tsMs, opusBuffer) {
    const id = Buffer.from(speakerId, 'utf8');
    const head = Buffer.allocUnsafe(2);
    head.writeUInt16BE(id.length, 0);
    const ts = Buffer.alloc(8);
    ts.writeBigUInt64BE(BigInt(tsMs), 0);
    return Buffer.concat([head, id, ts, opusBuffer]);
  }

  function openStream(sessionId, { guildId, onError, onClose }) {
    // The API key is intentionally NOT put in the URL (query strings leak into
    // access/proxy logs). Instead we rely on the service's first-text-frame
    // auth fallback: connect with no `key` query param, then send exactly one
    // text frame `{"key": "..."}` as the very first message once the socket
    // opens, before anything else (see services/meeting's
    // src/api/routers/meetings.py `_authenticate_ws`/`stream_meeting`).
    const url = `${wsUrl}/meetings/${sessionId}/stream?guild_id=${encodeURIComponent(guildId)}`;
    const ws = new WebSocketImpl(url);

    let open = false;
    let dead = false;
    const queue = [];

    const onOpen = () => {
      open = true;
      // Auth frame MUST be sent first, before any queued control/audio frames.
      ws.send(JSON.stringify({ key: apiKey }));
      for (const data of queue.splice(0)) ws.send(data);
    };
    const onErrorEvent = (e) => {
      if (dead) return;
      dead = true;
      console.error('meeting stream error:', e?.message ?? e);
      try {
        onError?.(e);
      } catch (cbErr) {
        console.error('meeting stream onError callback failed:', cbErr);
      }
    };
    // `ws.on('close', code, reason)` and addEventListener's CloseEvent carry
    // the same two fields in different shapes; openStream registers whichever
    // the impl supports, so normalize both rather than depending on one.
    const closeInfo = (codeOrEvent, reasonArg) => {
      const isEvent = codeOrEvent !== null && typeof codeOrEvent === 'object';
      const code = isEvent ? codeOrEvent.code : codeOrEvent;
      const rawReason = isEvent ? codeOrEvent.reason : reasonArg;
      const reason = rawReason?.toString?.() ?? '';
      return { code: code ?? 'unknown', reason: reason || '(none)' };
    };

    const onCloseEvent = (codeOrEvent, reasonArg) => {
      // A clean close is just as fatal as an error: every frame after this is
      // dropped by dispatch() below, silently. The surface needs to know so it
      // can stop the recorder rather than keep capturing into a dead socket.
      if (dead) return;
      dead = true;
      // ALWAYS log the close code. It is the only thing that says why a live
      // recording dropped, and discarding it here is what made two production
      // meetings fail with no diagnosable cause on either side of the wire.
      const { code, reason } = closeInfo(codeOrEvent, reasonArg);
      console.error(`meeting stream closed for session ${sessionId}: code=${code} reason=${reason}`);
      try {
        onClose?.({ code, reason });
      } catch (cbErr) {
        console.error('meeting stream onClose callback failed:', cbErr);
      }
    };

    if (typeof ws.addEventListener === 'function') {
      ws.addEventListener('open', onOpen);
      ws.addEventListener('error', onErrorEvent);
      ws.addEventListener('close', onCloseEvent);
    } else if (typeof ws.on === 'function') {
      ws.on('open', onOpen);
      ws.on('error', onErrorEvent);
      ws.on('close', onCloseEvent);
    }

    const dispatch = (data) => {
      if (dead) return;
      if (open) ws.send(data);
      else queue.push(data);
    };

    return {
      sendControl({ speakerId, displayName }) {
        dispatch(JSON.stringify({ speaker_id: speakerId, display_name: displayName }));
      },
      sendFrame(speakerId, tsMs, opusBuffer) {
        dispatch(encodeFrame(speakerId, tsMs, opusBuffer));
      },
      // Tell the service every audio frame has been sent. Goes through the same
      // dispatch path as the audio, so the socket delivers it AFTER all of it —
      // which is what lets the service use it as a barrier and stop finalizing
      // the meeting while the tail is still in flight.
      endAudio() {
        dispatch(JSON.stringify({ end_of_audio: true }));
      },
      close() {
        ws.close();
      },
    };
  }

  async function getTranscript(sessionId) {
    let resp;
    try {
      resp = await fetchImpl(`${baseUrl}/meetings/${sessionId}/transcript`, {
        headers: { 'X-API-Key': apiKey },
      });
    } catch {
      throw new MeetingUnavailable('network error reaching meeting service');
    }
    if (!resp.ok) throw new MeetingUnavailable(`meeting service returned ${resp.status}`);
    try {
      return await resp.json();
    } catch {
      throw new MeetingUnavailable('malformed meeting service response');
    }
  }

  async function stop(sessionId) {
    let resp;
    try {
      resp = await fetchImpl(`${baseUrl}/meetings/${sessionId}/stop`, {
        method: 'POST',
        headers: { 'X-API-Key': apiKey },
      });
    } catch {
      throw new MeetingUnavailable('network error reaching meeting service');
    }
    if (!resp.ok) throw new MeetingUnavailable(`meeting service returned ${resp.status}`);
    try {
      return await resp.json();
    } catch {
      throw new MeetingUnavailable('malformed meeting service response');
    }
  }

  return { encodeFrame, openStream, getTranscript, stop };
}
