# ECHO

**ECHO** is a self-hosted, OpenAI-compatible voice-to-text service that combines [Silero VAD](https://github.com/snakers4/silero-vad) with [whisper.cpp](https://github.com/ggerganov/whisper.cpp) to deliver fast, accurate, and hallucination-resistant speech transcription on local hardware.

- **Port 8001** — FastAPI service (VAD + proxy)
- **Port 8003** — whisper-server C++ binary (managed internally)
- **Drop-in** OpenAI `/v1/audio/transcriptions` API

---

## Quick Start

```bash
# 1. Clone the repo
git clone git@github.com:citadel/echo.git
cd echo

# 2. Create virtual environment and install dependencies
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt       # Windows
# source .venv/bin/activate && pip install -r requirements.txt  # macOS/Linux

# 3. Copy config and fill in your paths
cp config.json.example src/config.json
# Edit src/config.json: set whisper_server_path, whisper_model_path, etc.

# 4. Run locally (development)
.\scripts\dev.ps1           # Windows PowerShell
# uvicorn main:app --host 0.0.0.0 --port 8001 --reload  # from src/

# 5. Verify health
curl http://localhost:8001/health
```

---

## Configuration

ECHO uses a three-layer config system (each layer overrides the previous):

| Priority | Source | Path |
|----------|--------|------|
| 1 (lowest) | Built-in defaults | `src/config_defaults.json` |
| 2 | Local override | `src/config.json` |
| 3 (highest) | Environment variables | `UPPER_CASE` of key name |

See [`config.json.example`](config.json.example) for all available options.

---

## API

### `GET /health`

Returns service status.

```json
{ "status": "ok", "backend": "ok", "vad": "enabled" }
```

### `POST /v1/audio/transcriptions`

OpenAI-compatible transcription endpoint.

| Field | Type | Notes |
|-------|------|-------|
| `file` | WAV audio | Required. 16kHz mono recommended. |
| `model` | string | `whisper-1` (ignored, for API compat) |
| `language` | string | Default: `en` |
| `temperature` | string | Default: `0.0` |
| `prompt` | string | Optional context hint |

**Auth**: Set `VOICE_BEARER_TOKEN` env var or `bearer_token` in config. Pass as `Authorization: Bearer <token>`.

---

## Testing

```bash
# Unit tests (mocks all external I/O)
pytest tests/ -v

# Full CI gate (lint + format + tests)
.\scripts\test-ci.ps1

# Post-deployment smoke test (runs against live service)
.\scripts\test-host.ps1 -BaseUrl http://your-host:8001 -Token your-token
```

---

## macOS Deployment (launchd)

```bash
# Install unified ECHO service (replaces com.citadel.voice + com.citadel.whisper)
sudo cp com.citadel.echo.plist /Library/LaunchDaemons/
sudo launchctl load /Library/LaunchDaemons/com.citadel.echo.plist

# Check status
sudo launchctl list | grep echo

# View logs
tail -f /Users/homelab/echo/logs/echo.log
```

> **Note:** `com.citadel.echo.plist` supersedes the legacy `com.citadel.voice.plist` and `com.citadel.whisper.plist`. Unload and remove those before loading the new one.

---

## Architecture

See [`docs/architecture.md`](docs/architecture.md) for a detailed breakdown.
