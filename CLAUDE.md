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
                   ├── agent send (httpx async POST)
                   ├── WebSocket notification listener (asyncio.Task)
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
| `dispatch/agents/openclaw.py` | `OpenClawAgent` -- POST /v1/responses, WebSocket subscribe |
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
- **WebSocket auto-reconnect**: `async for ws in websockets.connect(uri)` handles network drops automatically.
- **No audio on disk**: mic frames processed in-place, TTS goes to BytesIO.
- **Single audio capture**: one pvrecorder instance shared via state machine -- never two mic readers.

## Credentials strategy

Two-layer split (12-Factor pattern). `agents.yaml` holds structural config and references env var **names** for secrets (e.g., `token_env: OPENCLAW_TOKEN`). `.env` holds the actual secret values, loaded at startup via `python-dotenv`. Never put secret values in `agents.yaml`.

Validation is contextual: `PICOVOICE_ACCESS_KEY` and `GOOGLE_APPLICATION_CREDENTIALS` are only required outside debug mode. Agent tokens are warned about but don't block startup (the agent will fail at runtime instead).

## OpenClaw API contract

Request: `POST /v1/responses` with `{"model": "openclaw", "input": "<text>"}`, header `Authorization: Bearer <token>`. Non-streaming.

Response: text is in `output[].content[].text`:
```json
{
  "output": [
    {
      "content": [
        {"text": "The response text here."}
      ]
    }
  ]
}
```

Health check: `GET /healthz` (called during `connect()`). Failure logs a warning, does not crash.

WebSocket notifications: `ws://localhost:18789?token=...`. Messages are JSON with `{"type": "response", "text": "...", "urgent": bool}`.

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
