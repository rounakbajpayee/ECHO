# ECHO

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/)
[![Coverage](https://img.shields.io/badge/coverage-90%25-success.svg)](https://github.com)


Fast, private, hallucination-resistant speech transcription for your home lab — OpenAI-compatible API, runs entirely on local hardware.

## Features

- **OpenAI-compatible** — drop-in replacement for `/v1/audio/transcriptions`; works with any client that speaks the OpenAI STT API
- **VAD gate** — Silero VAD filters silent or below-threshold clips before they touch the C++ model, eliminating spurious transcriptions
- **Hallucination blocking** — blocklist of known Whisper artifacts ("Thanks for watching.", "Subscribe.", etc.) filtered post-transcription
- **Unified lifecycle** — one service manages both the Python VAD layer and the `whisper-server` C++ subprocess; one plist, one restart
- **Zero-sudo deployment** — runs as a macOS LaunchAgent under your user session; no root required to start, stop, or update
- **Three-layer config** — `config_defaults.json` → `config.json` → env vars; override anything at any level without touching code

## Tech Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| STT engine | [whisper.cpp](https://github.com/ggerganov/whisper.cpp) | Preserves native C++ inference speed on Apple Silicon; no Python overhead on the hot path |
| VAD | [Silero VAD v6](https://github.com/snakers4/silero-vad) (ONNX) | Lightweight, CPU-only, runs in ~1ms per chunk; auto-detects model format at runtime |
| Service layer | FastAPI + uvicorn | Async-first; matches the OpenAI endpoint shape exactly |
| Proxy | httpx async | Non-blocking forwarding to the C++ backend |
| Deployment | macOS LaunchAgent | Preferred over LaunchDaemon — no sudo, restarts with user session |

---

## Quick Start

### Option 1: Docker (Recommended)

The FastAPI proxy and VAD layer can be run in Docker, while the actual `whisper-server` runs on your host machine to leverage bare-metal hardware acceleration (like Apple Silicon Metal).

1. Ensure `whisper-server` is running on your host at port 8003.
2. Run the proxy via Docker Compose:
```bash
git clone git@github.com:rounakbajpayee/ECHO.git
cd ECHO
docker-compose up -d
```

### Option 2: Local Python (Development)

```bash
# 1. Clone the repo
git clone git@github.com:rounakbajpayee/ECHO.git
cd ECHO

# 2. Create virtual environment and install dependencies
python -m venv .venv
.venv/Scripts/pip install -r requirements.lock     # Windows
# source .venv/bin/activate && pip install -r requirements.lock  # macOS/Linux

# 3. Copy config and fill in your paths
cp config.json.example src/config.json
# Edit src/config.json: set whisper_server_path, whisper_model_path

# 4. Run locally
.\scripts\dev.ps1           # Windows PowerShell
# cd src && uvicorn main:app --host 0.0.0.0 --port 8001 --reload  # macOS/Linux

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

## Dependencies

Dependency tracking uses lower bound versions in `requirements.txt` and `requirements-dev.txt`. We use `pip-tools` to generate fully pinned lockfiles for CI and production to ensure reproducibility.

To regenerate lockfiles after adding or updating dependencies:
```bash
pip-compile requirements.txt -o requirements.lock --no-header
pip-compile requirements-dev.txt -o requirements-dev.lock --no-header
```

---

## API

### `GET /health`

```json
{ "status": "ok", "backend": "ok", "vad": "enabled" }
```

### `POST /v1/audio/transcriptions`

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

# Post-deployment smoke test (runs against live service, no venv needed)
.\scripts\test-host.ps1 -BaseUrl http://your-host:8001 -Token your-token
```

---

## macOS Deployment (LaunchAgent)

```bash
# Install unified ECHO service (replaces com.citadel.voice + com.citadel.whisper)
mkdir -p ~/Library/LaunchAgents
cp com.citadel.echo.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.citadel.echo.plist

# Check status
launchctl list | grep echo

# View logs
tail -f /Users/homelab/echo/logs/echo.log
```

> **Note:** ECHO uses a user LaunchAgent (`~/Library/LaunchAgents/`) — no `sudo` required. `com.citadel.echo.plist` supersedes the legacy `com.citadel.voice.plist` and `com.citadel.whisper.plist` (those were LaunchDaemons). Unload and remove those with `sudo` before loading the new one.

---

## Architecture

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for a detailed breakdown and system diagrams.


## License

This project is licensed under the AGPLv3. For commercial use without open-sourcing your application, please contact the author to purchase a commercial license.
