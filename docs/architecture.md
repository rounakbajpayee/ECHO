# ECHO Architecture

## Overview

ECHO is a two-tier service:

```
Client (APRIL / any OpenAI-compat app)
        │  POST /v1/audio/transcriptions
        ▼
┌─────────────────────────────────────┐
│  ECHO FastAPI (port 8001)           │
│  ┌──────────────────────────────┐   │
│  │  Auth Check                  │   │
│  │  Head Trim (configurable ms) │   │
│  │  Silero VAD (Disabled by     │   │  ◄── Optional; if enabled & speech < min_ms → return {"text":""}
│  │          default)            │   │
│  └──────────────────────────────┘   │
│        │ audio                      │
│        ▼                            │
│  httpx async proxy                  │
└─────────────────────────────────────┘
        │  POST /v1/audio/transcriptions
        ▼
┌─────────────────────────────────────┐
│  whisper-server C++ (port 8003)     │
│  (bound to 127.0.0.1 only)          │
│  Model: ggml-large-v3-turbo         │
└─────────────────────────────────────┘
        │  {"text": "..."}
        ▼
ECHO: hallucination filter → return to client
```

---

## Components

### FastAPI App (`src/main.py`)

| Layer | Description |
|-------|-------------|
| **Lifespan** | Spawns `whisper-server` on startup, terminates on shutdown |
| **Config** | 3-layer system: defaults → `config.json` → env vars |
| **Auth** | Optional Bearer token (`VOICE_BEARER_TOKEN` / `bearer_token`) |
| **Head Trim** | Strips the first N ms from WAV to cut mic click noise |
| **Silero VAD** | ONNX inference, chunk-based speech probability scoring |
| **Proxy** | `httpx.AsyncClient` forwards audio to whisper-server |
| **Hallucination Filter** | Blocklist of known Whisper hallucinations (e.g. "Thanks for watching.") |

### whisper-server (C++ binary)

- Built from [whisper.cpp](https://github.com/ggerganov/whisper.cpp)
- Exposes the same `/v1/audio/transcriptions` endpoint
- Bound to `127.0.0.1` only — not reachable externally
- Managed by ECHO's lifespan (spawned/killed automatically)

---

## Data Flow

1. Client uploads WAV audio
2. ECHO checks Bearer token (if configured)
3. WAV head is trimmed (`head_trim_ms` from config)
4. If VAD is enabled (`vad_enabled: true`), Silero VAD runs on 16kHz float32 audio in chunks. If total speech < `vad_min_speech_duration_ms` → return `{"text": ""}` immediately.
5. Otherwise (or if VAD is disabled, which is the default), audio is proxied directly to `whisper-server`.
7. Response text is checked against the hallucination blocklist
8. Cleaned text returned to client

---

## Config System

```
src/config_defaults.json     ← always loaded
      ↓ override
src/config.json              ← local machine config (gitignored)
      ↓ override
UPPER_CASE env vars          ← highest priority (used in plist / launchd)
```

Legacy env vars `VOICE_BEARER_TOKEN` and `WHISPER_BACKEND_URL` are also supported for backward compatibility.

---

## Port Assignments

| Port | Service | Binding |
|------|---------|---------|
| 8001 | ECHO FastAPI | `0.0.0.0` (LAN-accessible) |
| 8003 | whisper-server C++ | `127.0.0.1` (loopback only) |

---

## VAD Strategy

Silero VAD v6 (ONNX) is used in a universal wrapper that handles both the older 4-input LSTM format and the newer 2-input state format by inspecting `get_inputs()` at runtime. This avoids tying ECHO to a specific model version.

Speech is quantified as total milliseconds above a probability threshold (`vad_threshold`, default `0.5`) across 32ms chunks. Clips with less than `vad_min_speech_duration_ms` (default 200ms) are silently dropped.
