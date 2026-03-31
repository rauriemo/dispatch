# Dispatch

Modular voice-first command channel for AI agents. Listens for wake words via Picovoice Porcupine, routes each to a different AI agent backend, transcribes speech via Google Cloud STT, sends transcripts to the matched agent, speaks responses via Edge TTS, and receives push notifications from agents via WebSocket. Toggled on/off with a global hotkey and system tray icon.

## Architecture

### AudioPipeline state machine

A single `pvrecorder` instance captures mic frames in a background thread. Frames are routed by state:

```
LISTENING  -- frames go to Porcupine.process(), waiting for wake word
RECORDING  -- frames go to queue.Queue, consumed by STT thread
PAUSED     -- frames discarded (TTS playing, or system toggled off)
```

Transitions: LISTENING->RECORDING on wake word detection, RECORDING->LISTENING when STT returns, any->PAUSED on toggle off or TTS start, PAUSED->LISTENING on toggle on or TTS end.

### AgentRouter

Maps wake-word keyword indices to agent instances. A type registry (`{"openclaw": OpenClawAgent, ...}`) instantiates agents from `agents.yaml` config. Async context manager -- `__aenter__` connects all agents, `__aexit__` disconnects them. If an agent fails to connect at startup, it logs a warning and continues degraded.

### Debug mode

`--debug` swaps `AudioPipeline` for `DebugPipeline` (Enter key simulates wake word) and `stream_transcribe` for `debug_transcribe` (typed input). Same interfaces, so `main.py` never branches. Runs the full pipeline without Picovoice or Google Cloud accounts.

## Threading model

```
Main thread        asyncio event loop (main.py run())
                   ├── wake word listen (awaits asyncio.Event)
                   ├── TTS playback (edge-tts async stream + pygame poll)
                   ├── agent send (WebSocket JSON frame)
                   ├── WebSocket recv loop (asyncio.Task, auto-reconnect)
                   └── notification drain loop

Capture thread     threading.Thread (AudioPipeline._capture_loop)
                   └── pvrecorder.read() in tight loop, routes frames by state

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
| `dispatch/audio.py` | `AudioPipeline` (pvrecorder + Porcupine state machine), `DebugPipeline`, chime generation |
| `dispatch/stt.py` | `stream_transcribe` (Google Cloud STT streaming), `debug_transcribe` (typed input) |
| `dispatch/tts.py` | `speak()` -- edge-tts to BytesIO, pygame playback |
| `dispatch/config.py` | `DispatchConfig`/`AgentConfig` dataclasses, YAML + .env loading, validation |
| `dispatch/notifications.py` | `Notification` dataclass, `NotificationQueue` (asyncio.PriorityQueue wrapper) |
| `dispatch/agents/base.py` | `BaseAgent` ABC, `AgentError`, `AgentRouter` (type registry + routing) |
| `dispatch/agents/openclaw.py` | `OpenClawAgent` -- WebSocket gateway (chat + notifications), auto-reconnect |
| `dispatch/crypto.py` | Ed25519 device identity for OpenClaw gateway handshake |
| `dispatch/__main__.py` | Entry point, parses `--debug` flag |
| `agents.yaml` | Agent registry: type, wake word path, endpoint, token env var, TTS voice |
| `.env` | Secrets (gitignored): `PICOVOICE_ACCESS_KEY`, `OPENCLAW_TOKEN`, `GOOGLE_APPLICATION_CREDENTIALS` |

## How to run

```bash
# Debug mode -- keyboard input, no cloud/hardware deps needed
python -m dispatch --debug

# Live mode -- requires .env with PICOVOICE_ACCESS_KEY, OPENCLAW_TOKEN,
# GOOGLE_APPLICATION_CREDENTIALS, and a .ppn wake word file in assets/
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
- **Context managers**: `AgentRouter` (async with), `AudioPipeline`/`DebugPipeline` (with), `httpx.AsyncClient` -- guaranteed cleanup.
- **WebSocket auto-reconnect**: `_recv_loop` reconnects with exponential backoff (1–30s) on disconnect, re-handshakes, and resumes frame processing.
- **No audio on disk**: mic frames processed in-place, TTS goes to BytesIO.
- **Single audio capture**: one pvrecorder instance shared via state machine -- never two mic readers.

## Credentials strategy

Two-layer split (12-Factor pattern). `agents.yaml` holds structural config and references env var **names** for secrets (e.g., `token_env: OPENCLAW_TOKEN`). `.env` holds the actual secret values, loaded at startup via `python-dotenv`. Never put secret values in `agents.yaml`.

Validation is contextual: `PICOVOICE_ACCESS_KEY` and `GOOGLE_APPLICATION_CREDENTIALS` are only required outside debug mode. Agent tokens are warned about but don't block startup (the agent will fail at runtime instead).

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

Health check: `GET /healthz` (called during `connect()`). Failure logs a warning, does not crash.

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
| Navi (OpenClaw) | `en-US-GuyNeural` | Calm, informational male |
| (future) | `en-US-AriaNeural` | Friendly, expressive female |
| (future) | `en-US-EricNeural` | Deep, authoritative male |
| (future) | `en-US-JennyNeural` | Warm, conversational female |

Full catalog: `edge-tts --list-voices`. Swap any voice by editing one line in `agents.yaml`.

## Notification priority model

`Notification` is a `@dataclass(order=True)` with priority field: `0` = urgent, `1` = normal (lower number = higher priority). Ties broken by timestamp.

`NotificationQueue` wraps `asyncio.PriorityQueue` -- both producer (WebSocket task) and consumer (main loop) are async on the same event loop, so no thread boundary.

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
    endpoint: http://localhost:9999
    token_env: MYAGENT_TOKEN
    voice: en-US-AriaNeural
```

4. Add the token to `.env` and the `.ppn` wake word file to `assets/`.
