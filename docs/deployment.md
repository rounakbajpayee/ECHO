# ECHO Deployment Guide

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| macOS (Apple Silicon or Intel) | Primary deployment target |
| Python 3.11+ | For the FastAPI service |
| whisper.cpp built binary | See build instructions below |
| Whisper model file | `.bin` format (ggml) |
| Silero VAD model | Auto-downloaded on first run |

---

## 1. Build whisper-server (One-Time)

```bash
# Clone whisper.cpp
git clone https://github.com/ggerganov/whisper.cpp
cd whisper.cpp

# Download a model
./models/download-ggml-model.sh large-v3-turbo

# Build the server binary
cmake -B build
cmake --build build --config Release -j$(nproc)

# Binary will be at: build/bin/whisper-server
```

---

## 2. Install ECHO

> [!IMPORTANT]
> **Git Authentication on Host:**
> To ensure that the non-interactive CD pipeline can pull code successfully without getting blocked by username/password prompts:
> - **Option A (Recommended):** Clone the repository using the **SSH URL** (e.g. `git@github.com:rounakbajpayee/ECHO.git`), which uses SSH keys to authenticate automatically.
> - **Option B:** If using **HTTPS**, configure Git to store credentials before cloning:
>   `git config --global credential.helper store`
>   Then run a manual `git fetch` once and enter your GitHub username and Personal Access Token (PAT). Git will cache it permanently.

```bash
# 1. Create logs directory
mkdir -p /Users/homelab/echo/logs

# 2. Clone the repository (Use SSH URL or configure credential helper first)
git clone git@github.com:rounakbajpayee/ECHO.git /Users/homelab/echo
cd /Users/homelab/echo

# 3. Create virtual environment
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 4. Copy and configure
cp config.json.example src/config.json
```

Edit `src/config.json` and set:
- `whisper_server_path`: absolute path to the `whisper-server` binary
- `whisper_model_path`: absolute path to the `.bin` model
- `bearer_token`: optional auth token
- `spawn_whisper_server`: `true` (ECHO manages the C++ server lifecycle)

---

## 3. Configure launchd (LaunchAgent)

ECHO runs as a **LaunchAgent** — it lives in `~/Library/LaunchAgents/` and runs under your user session. No `sudo` required.

```bash
# Create LaunchAgents dir if it doesn't exist
mkdir -p ~/Library/LaunchAgents

# Copy the plist
cp /Users/homelab/echo/com.citadel.echo.plist ~/Library/LaunchAgents/

# Load and start
launchctl load ~/Library/LaunchAgents/com.citadel.echo.plist
```

---

## 4. Verify Deployment

```bash
# Quick health check
curl http://localhost:8001/health

# Full post-deployment test (from Windows Lenovo or macOS):
.\scripts\test-host.ps1 -BaseUrl http://<host-ip>:8001
```

Expected health output:
```json
{"status": "ok", "backend": "ok", "vad": "enabled"}
```

---

## 5. APRIL Integration

Update APRIL's config to point to ECHO:

```json
{
  "stt_url": "http://<echo-host-ip>:8001/v1/audio/transcriptions",
  "stt_bearer_token": "<your-token>"
}
```

---

## Network Layout

```
┌──────────────────┐        LAN         ┌──────────────────────────────┐
│  Windows Lenovo  │ ──────────────────► │  macOS Server (ECHO :8001)  │
│  (APRIL)         │  POST /v1/audio/   │                              │
│                  │  transcriptions    │  → whisper-server :8003      │
└──────────────────┘                    │     (localhost only)         │
                                        └──────────────────────────────┘
```

---

## Environment Variable Reference

All config keys can be overridden via uppercase env vars. Key ones for the plist:

| Env Var | Default | Description |
|---------|---------|-------------|
| `SPAWN_WHISPER_SERVER` | `true` | Whether ECHO manages the C++ binary |
| `WHISPER_BACKEND_URL` | `http://127.0.0.1:8003` | URL of whisper-server |
| `BACKEND_TIMEOUT` | `30.0` | Timeout in seconds for C++ requests |
| `HEAD_TRIM_MS` | `50` | Milliseconds to trim from WAV head |
| `VAD_ENABLED` | `true` | Enable/disable Silero VAD |
| `VAD_THRESHOLD` | `0.5` | Speech probability threshold (0–1) |
| `VAD_MIN_SPEECH_DURATION_MS` | `200` | Minimum ms of speech to forward |
| `VOICE_BEARER_TOKEN` | `""` | Bearer token for auth (legacy name) |
