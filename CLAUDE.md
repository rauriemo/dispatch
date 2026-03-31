# Dispatch

Modular voice-first command channel for AI agents. Listens for wake words via Picovoice Porcupine, routes each to a different AI agent backend, transcribes speech via Google Cloud STT, sends transcripts to the matched agent, speaks responses via Edge TTS, and receives push notifications from agents via WebSocket. Toggled on/off with a global hotkey and system tray icon.

## Architecture

### Pipeline selection (three-tier)

Dispatch selects its audio pipeline automatically based on available credentials:

```
PICOVOICE_ACCESS_KEY set?         -> AudioPipeline (Porcupine, local, fast)
No Picovoice, GOOGLE_APPLICATION_CREDENTIALS set? -> STTWakePipeline (Google STT, cloud)
Neither, or --debug flag          -> DebugPipeline (keyboard input)
```

All three pipelines expose the same interface (`listen()`, `set_state()`, `pause()`, `resume()`, context manager, `frame_queue`) so `main.py` never branches on pipeline type.

### AudioPipeline state machine

A single `pvrecorder` instance captures mic frames in a background thread. Frames are routed by state:

```
LISTENING  -- frames go to Porcupine.process(), waiting for wake word
RECORDING  -- frames go to queue.Queue, consumed by STT thread
PAUSED     -- frames discarded (TTS playing, or system toggled off)
```

Transitions: LISTENING->RECORDING on wake word detection, RECORDING->LISTENING when STT returns, any->PAUSED on toggle off or TTS start, PAUSED->LISTENING on toggle on or TTS end.

### STTWakePipeline (Google STT fallback)

When Picovoice is unavailable but Google Cloud credentials exist, `STTWakePipeline` provides voice-based wake word detection using Google Cloud STT. Same state machine as `AudioPipeline` but replaces Porcupine with continuous STT transcription and text matching.

A background thread runs pvrecorder (no access key needed) + Google STT in a loop:
- In LISTENING state: frames stream to `streaming_recognize(single_utterance=True)`. Each stream captures one spoken phrase, then the transcript is checked against registered wake phrases.
- If matched: chime plays, `keyword_index` and optional `pending_command` are stored, state transitions to RECORDING, async wake event fires.
- If no match: loop starts a new STT stream immediately.
- In RECORDING state: frames go to `frame_queue` for command transcription.
- In PAUSED state: frames discarded.

**Single-utterance support:** "Hey navi, what's the weather?" is handled in one shot. The wake phrase is matched and the command text after it is extracted into `pending_command`. The main loop checks `pending_command` before calling the transcribe function -- if present, it skips the second STT call entirely.

**Wake phrase matching:** case-insensitive, strips punctuation between phrase and command. Phrases are configured via `wake_phrase` in `agents.yaml` (auto-derived from .ppn filename if omitted).

**Cost:** Google STT streams continuously while LISTENING (~$0.006/15s). Toggle Dispatch off when not in use.

### AgentRouter

Maps wake-word keyword indices to agent instances. A type registry (`{"openclaw": OpenClawAgent, ...}`) instantiates agents from `agents.yaml` config. Async context manager -- `__aenter__` connects all agents, `__aexit__` disconnects them. If an agent fails to connect at startup, it logs a warning and continues degraded.

### Dual-connection architecture (OpenClaw)

Each OpenClawAgent opens two WebSocket connections to the same gateway endpoint:

1. **Operator connection** (`client.mode: "cli"`, `role: "operator"`) -- sends `chat.send` requests and receives streaming response events. This is the interactive chat path.
2. **Node connection** (`client.mode: "node"`, `role: "node"`, `caps: ["voice"]`) -- registers Dispatch as a voice-capable device. The gateway can invoke `voice.speak` on this connection to deliver proactive messages without a prior user request.

Both connections use the same device keypair and auto-reconnect independently. If the node connection fails (e.g., gateway rejects the client ID), the operator connection still works -- proactive push is degraded but chat is unaffected. Unrequested events on the operator connection are also routed to the notification queue as a fallback.

### Webhook endpoint (scheduled delivery)

Dispatch runs a lightweight `aiohttp.web` HTTP server on `127.0.0.1` (localhost only) to receive notifications from external sources like OpenClaw cron jobs. This bridges the gap where scheduled agent turns run in an isolated context and cannot invoke `voice.speak` on the node WebSocket.

Three notification delivery paths, all converging on the same `NotificationQueue`:

```
1. Interactive  -- user speaks -> agent responds via operator WebSocket -> _handle_event
2. Live push    -- agent invokes voice.speak on node WebSocket -> _handle_invoke
3. Scheduled    -- cron job POSTs to webhook endpoint -> webhook handler
```

Webhook details:
- `POST /notify` accepts `{"agent": "navi", "text": "...", "priority": 1}`, validates the payload, looks up the agent's voice from a name-to-voice dict, creates a `Notification`, and pushes it to the `NotificationQueue`.
- Optional auth via `DISPATCH_WEBHOOK_SECRET` env var (checked against `Authorization: Bearer <secret>` header).
- Port configured via `webhook_port` in `agents.yaml` settings (0 = disabled, default 18790).
- If the server fails to bind, Dispatch logs a warning and continues with webhook delivery unavailable (consistent degraded-mode pattern).

This is **opt-in per cron job**: only reminders the user explicitly asks to be delivered via Dispatch should target this endpoint. The routing decision lives on the OpenClaw side at cron creation time, not in Dispatch. Dispatch is a passive receiver -- if nothing POSTs to it, nothing plays.

### Debug mode

`--debug` swaps `AudioPipeline` for `DebugPipeline` (Enter key simulates wake word) and `stream_transcribe` for `debug_transcribe` (typed input). Same interfaces, so `main.py` never branches. Runs the full pipeline without Picovoice or Google Cloud accounts.

## Threading model

```
Main thread        asyncio event loop (main.py run())
                   ├── wake word listen (awaits asyncio.Event)
                   ├── TTS playback (edge-tts async stream + pygame poll)
                   ├── agent send (WebSocket JSON frame, operator connection)
                   ├── operator recv loop (asyncio.Task, auto-reconnect)
                   ├── node recv loop (asyncio.Task, auto-reconnect, voice invokes)
                   ├── webhook server (aiohttp on 127.0.0.1, POST /notify)
                   └── notification drain loop

Capture thread     threading.Thread (AudioPipeline._capture_loop or STTWakePipeline._stt_watch_loop)
                   └── pvrecorder.read() in tight loop, routes frames by state
                   └── (STTWake) also runs streaming_recognize() in the same thread

STT thread         asyncio.to_thread(_blocking_transcribe)
                   └── google.cloud.speech streaming_recognize() (blocking gRPC)

Hotkey thread      pynput GlobalHotKeys.start() (daemon thread)
                   └── signals asyncio via loop.call_soon_threadsafe()

Tray thread        pystray Icon.run() (daemon thread, blocking)
```

The frame queue is **stdlib `queue.Queue`**, not `asyncio.Queue`. Both the audio capture thread (pvrecorder.read) and the STT thread (blocking gRPC via asyncio.to_thread) are sync contexts. Using asyncio.Queue from a thread would corrupt data. The capture thread signals the asyncio loop via `loop.call_soon_threadsafe(event.set)`.

## Key files

| File | Owns |
|---|---|
| `dispatch/main.py` | Hotkey toggle, system tray, main voice-command loop, shutdown |
| `dispatch/audio.py` | `AudioPipeline` (pvrecorder + Porcupine), `STTWakePipeline` (pvrecorder + Google STT), `DebugPipeline`, chime generation |
| `dispatch/stt.py` | `stream_transcribe` (Google Cloud STT streaming), `debug_transcribe` (typed input) |
| `dispatch/tts.py` | `speak()` -- edge-tts to BytesIO, pygame playback |
| `dispatch/config.py` | `DispatchConfig`/`AgentConfig` dataclasses, YAML + .env loading, validation, wake phrase derivation |
| `dispatch/notifications.py` | `Notification` dataclass, `NotificationQueue` (asyncio.PriorityQueue wrapper) |
| `dispatch/agents/base.py` | `BaseAgent` ABC, `AgentError`, `AgentRouter` (type registry + routing) |
| `dispatch/agents/openclaw.py` | `OpenClawAgent` -- dual WebSocket (operator chat + node voice), auto-reconnect |
| `dispatch/crypto.py` | Ed25519 device identity for OpenClaw gateway handshake |
| `dispatch/webhook.py` | `aiohttp.web` server -- `POST /notify` endpoint for cron/scheduled delivery |
| `dispatch/__main__.py` | Entry point, parses `--debug` flag |
| `agents.yaml` | Agent registry: type, wake word path, wake phrase, endpoint, token env var, TTS voice |
| `.env` | Secrets (gitignored): `PICOVOICE_ACCESS_KEY`, `OPENCLAW_TOKEN`, `GOOGLE_APPLICATION_CREDENTIALS`, `DISPATCH_WEBHOOK_SECRET` |

## How to run

```bash
# Debug mode -- keyboard input, no cloud/hardware deps needed
python -m dispatch --debug

# Live mode (Picovoice) -- best wake word detection, requires PICOVOICE_ACCESS_KEY
python -m dispatch

# Live mode (STT wake) -- no Picovoice key needed, uses Google STT for wake detection
# Just set GOOGLE_APPLICATION_CREDENTIALS (no PICOVOICE_ACCESS_KEY)
python -m dispatch
```

Install deps first: `pip install -r requirements.txt`

## Critical implementation details

- **Frame queue**: `queue.Queue` (stdlib), never `asyncio.Queue`. Both the capture thread and STT thread are sync contexts.
- **Blocking gRPC**: Google STT `streaming_recognize()` runs via `asyncio.to_thread()`. pvrecorder `read()` runs in a `threading.Thread`. Neither blocks the event loop.
- **Frame format**: pvrecorder returns `list[int]` (int16). Porcupine accepts this directly. STT needs bytes -- `struct.pack(f'<{len(frame)}h', *frame)`.
- **pygame TTS load**: `pygame.mixer.music.load(buffer, "mp3")` -- the `"mp3"` string is required for BytesIO MP3 data. Without it pygame fails silently.
- **BytesIO seek**: `buffer.seek(0)` before `pygame.mixer.music.load()`. After collecting edge-tts chunks the cursor is at end -- without seek, pygame reads zero bytes.
- **pygame mixer init**: `frequency=44100, size=-16, channels=2, buffer=2048`. Stereo (channels=2) because edge-tts MP3 may be stereo.
- **Hotkey format**: pynput requires angle brackets: `<ctrl>+<shift>+n`. Stored in this exact format in `agents.yaml`. Malformed strings are silently ignored.
- **Chime**: 150ms 880Hz sine wave via `array.array('h')` + `math.sin()`, loaded as `pygame.mixer.Sound(buffer=...)`. No numpy.
- **STTWakePipeline**: uses Google STT `streaming_recognize(single_utterance=True)` for wake phrase detection. Runs pvrecorder + STT in one thread. `pending_command` holds the extracted command from single-utterance detection -- main loop checks it before calling `transcribe_fn`. Exponential backoff (1-30s) on STT stream errors.
- **Wake phrase config**: `AgentConfig.wake_phrase` is auto-derived from the `.ppn` filename (`assets/hey-navi.ppn` -> `"hey navi"`). Explicit override via `wake_phrase:` in `agents.yaml`. Platform suffixes (`_en_windows`, etc.) are stripped during derivation.
- **Context managers**: `AgentRouter` (async with), `AudioPipeline`/`STTWakePipeline`/`DebugPipeline` (with), `httpx.AsyncClient` -- guaranteed cleanup.
- **WebSocket auto-reconnect**: both `_recv_loop` (operator) and `_node_recv_loop` (node) reconnect independently with exponential backoff (1–30s) on disconnect, re-handshake, and resume frame processing.
- **Unrequested events**: `chat` events with `state: "final"` whose `runId` doesn't match a pending request are treated as proactive push messages and routed to the notification queue.
- **Webhook server**: `aiohttp.web` on `127.0.0.1` only (never `0.0.0.0`). Disabled when `webhook_port` is 0. Auth is optional (`DISPATCH_WEBHOOK_SECRET` env var). The agent-name-to-voice lookup dict is built from the `AgentRouter`'s agent list at startup.
- **No audio on disk**: mic frames processed in-place, TTS goes to BytesIO.
- **Single audio capture**: one pvrecorder instance shared via state machine -- never two mic readers.

## Credentials strategy

Two-layer split (12-Factor pattern). `agents.yaml` holds structural config and references env var **names** for secrets (e.g., `token_env: OPENCLAW_TOKEN`). `.env` holds the actual secret values, loaded at startup via `python-dotenv`. Never put secret values in `agents.yaml`.

Validation is contextual: `PICOVOICE_ACCESS_KEY` and `GOOGLE_APPLICATION_CREDENTIALS` are only required outside debug mode. Agent tokens are warned about but don't block startup (the agent will fail at runtime instead). `DISPATCH_WEBHOOK_SECRET` is optional -- if unset, the webhook endpoint accepts unauthenticated requests (acceptable for localhost-only).

## OpenClaw API contract

### HTTP endpoint (currently blocked by gateway scope bug)

The HTTP `POST /v1/responses` endpoint exists and is enabled, but returns `403 {"ok":false,"error":{"type":"forbidden","message":"missing scope: operator.write"}}` due to a token-mode scope restoration bug in OpenClaw 2026.3.30. This is a known issue (GitHub #46650). Keep the HTTP code as a fallback for when it's fixed.

### WebSocket gateway protocol (primary transport)

OpenClaw's native transport is WebSocket with JSON text frames. Full docs: https://docs.clawd.bot/gateway/protocol

**Handshake flow:**

1. Connect to `ws://<endpoint>` (same host:port as HTTP)
2. Gateway sends challenge: `{"type":"event","event":"connect.challenge","payload":{"nonce":"...","ts":...}}`
3. Client sends connect request:
```json
{
  "type": "req",
  "id": "<uuid>",
  "method": "connect",
  "params": {
    "minProtocol": 3,
    "maxProtocol": 3,
    "client": {"id": "cli", "version": "0.1.0", "platform": "windows", "mode": "cli"},
    "role": "operator",
    "scopes": ["operator.read", "operator.write"],
    "caps": [],
    "commands": [],
    "permissions": {},
    "auth": {"token": "<gateway_token>"},
    "locale": "en-US",
    "userAgent": "dispatch/0.1.0",
    "device": {
      "id": "<fingerprint_of_public_key>",
      "publicKey": "<base64_ed25519_public_key>",
      "signature": "<base64_signature_of_challenge>",
      "signedAt": <timestamp_ms>,
      "nonce": "<nonce_from_challenge>"
    }
  }
}
```
4. Gateway responds: `{"type":"res","id":"...","ok":true,"payload":{"type":"hello-ok","protocol":3,"policy":{"tickIntervalMs":15000}}}`

**Device identity:**
- Generate an Ed25519 keypair (persist in `.dispatch_device_key` or similar, gitignored)
- `device.id` = SHA-256 hex fingerprint of the raw public key bytes
- `device.publicKey` = base64url-encoded (no padding) raw public key
- `device.signature` = Ed25519 signature of the v2 payload, base64url-encoded (no padding)
- `device.signedAt` = millisecond timestamp used in the payload
- `device.nonce` = the nonce from the challenge

**Signature payload (v2):** pipe-delimited string signed with the device private key:
```
v2|{deviceId}|{clientId}|{clientMode}|{role}|{scopes}|{signedAtMs}|{token}|{nonce}
```
Example: `v2|abc123...|cli|cli|operator|operator.read,operator.write|1711856400000|mytoken|server-nonce`
- `scopes` = comma-joined (no spaces)
- `token` = the auth token being sent (empty string if none)
- All values must match the corresponding connect params exactly

**Sending a message (after handshake):**

Request frame:
```json
{"type":"req","id":"<uuid>","method":"chat.send","params":{"sessionKey":"<session_uuid>","message":"<user_message>","idempotencyKey":"<uuid>"}}
```

The `sessionKey` groups messages into a conversation (client-generated UUID, persists per agent instance). The `idempotencyKey` prevents duplicate processing (use the request frame `id`).

Response flow -- gateway sends `res` with `ok: true` and `status: "started"`, then streams:
- `event: "agent"` with `payload.stream: "assistant"` and `payload.data.delta` -- streaming text chunks
- `event: "chat"` with `payload.state: "final"` and `payload.message.content[].text` -- final complete message
- Correlation key: `payload.runId` (matches the request frame `id`)

**Message framing:**
- Request: `{"type":"req","id":"<uuid>","method":"<method>","params":{...}}`
- Response: `{"type":"res","id":"<uuid>","ok":true,"payload":{...}}`
- Event: `{"type":"event","event":"<name>","payload":{...}}`

**Auth:**
- `gateway.auth.mode` is `token` -- `connect.params.auth.token` must match the gateway token
- After successful connect, scopes declared in `connect.params.scopes` are granted
- Device tokens may be issued in `hello-ok.auth.deviceToken` for future connects

**First-run device pairing:**
On first connect, the gateway returns `PAIRING_REQUIRED` with a `requestId`. Approve it on the gateway host:
```bash
openclaw devices approve <requestId>
# or inside Docker: docker exec -it <container> openclaw devices approve <requestId>
```
After approval the device key is trusted for future connects.

### Node connection (voice capability)

A second WebSocket connects as a node to receive proactive invocations from the gateway.

**Node connect params (differences from operator):**
```json
{
  "client": {"id": "node-host", "version": "0.1.0", "platform": "windows", "mode": "node"},
  "role": "node",
  "scopes": [],
  "caps": ["voice"],
  "commands": ["voice.speak"],
  "permissions": {"voice.speak": true}
}
```

The signature payload uses the same v2 format with the node's role/scopes/clientId/clientMode values. Same device keypair, so the gateway sees both connections as the same device with different roles.

**Invoke protocol:** The gateway sends a `req` frame to the node connection when the agent wants to speak:
```json
{"type":"req","id":"<uuid>","method":"invoke","params":{"command":"voice.speak","args":{"text":"..."}}}
```

Dispatch queues the text as an urgent notification (priority 0) and acks:
```json
{"type":"res","id":"<uuid>","ok":true,"payload":{}}
```

The main loop's notification drain picks it up and plays it via TTS. This is the path for proactive "computer messages" from the agent.

**First-run pairing:** The node connection requires a separate pairing approval since it connects with `role: "node"` (different from the operator pairing).

Health check: `GET /healthz` (called during `connect()`). Failure logs a warning, does not crash.

### Webhook endpoint contract

Dispatch listens on `http://127.0.0.1:<webhook_port>/notify` for scheduled delivery.

**Request:**
```
POST /notify HTTP/1.1
Content-Type: application/json
Authorization: Bearer <DISPATCH_WEBHOOK_SECRET>  (optional, only if secret is configured)

{"agent": "navi", "text": "Time for your standup!", "priority": 1}
```

- `agent` (required): agent name matching an entry in `agents.yaml`. Used to look up the TTS voice.
- `text` (required): the message to speak via TTS.
- `priority` (optional): 0 = urgent, 1 = normal. Defaults to 1.

**Responses:**
- `200 {"ok": true}` -- notification queued for TTS
- `400 {"ok": false, "error": "..."}` -- missing/invalid fields or malformed JSON
- `401 {"ok": false, "error": "unauthorized"}` -- wrong or missing auth token (when secret is configured)
- `404 {"ok": false, "error": "unknown agent"}` -- agent name not found in registry

**OpenClaw cron setup (agent-side, not Dispatch code):**

Only reminders the user explicitly requests for voice delivery should target this endpoint. Example cron creation on the OpenClaw side:
```
/cron add --every 30m --webhook http://localhost:18790/notify --payload '{"agent":"navi","text":"Check the deploy status"}'
```

The agent should recognize delivery intent keywords ("on Dispatch", "voice reminder", "speak it") and only configure the webhook URL for those cron jobs. Normal reminders without Dispatch mention should not target the webhook.

## Wake word constraints

- Picovoice recommends 6+ phonemes for reliable detection with minimal false positives
- Convention: "hey X" format (e.g., "hey navi" = 6 phonemes)
- `.ppn` models are trained in Picovoice Console and placed in `assets/`
- Free tier: 1 custom wake word model per month
- Built-in models (Alexa, Hey Siri, Hey Google, Ok Google) are free under Apache 2.0 from the Porcupine GitHub repo -- usable as placeholders while waiting for custom training slots
- The code is agnostic -- it loads whatever `.ppn` is configured

## Voice catalog

Each agent specifies an Edge TTS voice in `agents.yaml`. All en-US voices are free, no API key.

| Agent | Voice | Character |
|---|---|---|
| Navi (OpenClaw) | `en-US-AvaMultilingualNeural` | Friendly, expressive female |
| (future) | `en-US-AriaNeural` | Friendly, expressive female |
| (future) | `en-US-EricNeural` | Deep, authoritative male |
| (future) | `en-US-JennyNeural` | Warm, conversational female |

Full catalog: `edge-tts --list-voices`. Swap any voice by editing one line in `agents.yaml`.

## Notification priority model

`Notification` is a `@dataclass(order=True)` with priority field: `0` = urgent, `1` = normal (lower number = higher priority). Ties broken by timestamp.

`NotificationQueue` wraps `asyncio.PriorityQueue` -- all producers (operator recv loop, node recv loop, webhook handler) and the consumer (main loop drain) are async on the same event loop, so no thread boundary.

Playback rules:
- Agent name announced before every notification ("Navi says: ...")
- Multiple queued notifications play sequentially, never overlapping
- Notifications held while STT is recording or TTS is already playing
- Main loop drains the queue on every 2s listen-timeout cycle

## How to add a new agent

1. Create `dispatch/agents/myagent.py` implementing `BaseAgent`:

```python
from dispatch.agents.base import BaseAgent, AgentRouter

class MyAgent(BaseAgent):
    def __init__(self, name, voice, endpoint, token_env):
        super().__init__(name, voice)
        # setup client

    async def connect(self): ...
    async def disconnect(self): ...
    async def send(self, text: str) -> str: ...

AgentRouter.register("myagent", MyAgent)
```

2. Import it in `dispatch/agents/__init__.py`:

```python
import dispatch.agents.myagent  # noqa: F401
```

3. Add the agent to `agents.yaml`:

```yaml
agents:
  myagentname:
    type: myagent
    wake_word: assets/hey-myagent.ppn
    # wake_phrase: hey myagent  (auto-derived from wake_word if omitted)
    endpoint: http://localhost:9999
    token_env: MYAGENT_TOKEN
    voice: en-US-AriaNeural
```

4. Add the token to `.env` and the `.ppn` wake word file to `assets/`.
