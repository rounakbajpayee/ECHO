# ECHO Runbook

Operational procedures for managing the ECHO service in production.

---

## Service Status

```bash
# Check if launchd service is loaded and running
launchctl list | grep echo

# Expected output:
# <PID>   0    com.citadel.echo

# If PID is "-", the service is loaded but not running (likely crashed)
```

---

## Starting & Stopping

```bash
# Load and start
launchctl load ~/Library/LaunchAgents/com.citadel.echo.plist

# Stop and unload
launchctl unload ~/Library/LaunchAgents/com.citadel.echo.plist

# Restart (unload then reload)
launchctl unload ~/Library/LaunchAgents/com.citadel.echo.plist
launchctl load   ~/Library/LaunchAgents/com.citadel.echo.plist
```

---

## Viewing Logs

```bash
# Live service log
tail -f /Users/homelab/echo/logs/echo.log

# Error log (crash traces, startup failures)
tail -f /Users/homelab/echo/logs/echo.err

# Check last N lines
tail -n 100 /Users/homelab/echo/logs/echo.log
```

---

## Health Check

```bash
curl http://localhost:8001/health
# Expected: {"status":"ok","backend":"ok","vad":"enabled"}

# If backend is "unreachable":
# → whisper-server failed to start (check echo.err for binary/model path errors)
# → Wrong whisper_backend_url in config

# If vad is "disabled":
# → vad_model_path is wrong or model failed to download
# → Check echo.err for "Failed to load Silero VAD"
```

---

## Manual Smoke Test

```bash
# From the Lenovo (or any machine on LAN):
.\scripts\test-host.ps1 -BaseUrl http://<host-ip>:8001 -Token <your-token>

# Or with curl (no auth):
curl -X POST http://localhost:8001/v1/audio/transcriptions \
  -F "file=@/path/to/test.wav" \
  -F "model=whisper-1"
```

---

## whisper-server Not Starting

1. Check `echo.err` for the error message
2. Verify paths in `src/config.json`:
   - `whisper_server_path` → must point to the compiled binary
   - `whisper_model_path` → must point to a `.bin` model file
3. Verify binary is executable: `ls -l /path/to/whisper-server`
4. Try running the binary manually:
   ```bash
   /Users/homelab/whisper.cpp/build/bin/whisper-server \
     --model /Users/homelab/whisper.cpp/models/ggml-large-v3-turbo.bin \
     --host 127.0.0.1 --port 8003 --inference-path /v1/audio/transcriptions
   ```

---

## VAD Model Missing

ECHO auto-downloads the Silero VAD model on first startup if it's missing. If this fails (e.g. no internet access):

```bash
# Download manually:
curl -L -o /Users/homelab/echo/src/assets/silero_vad_v6.onnx \
  https://raw.githubusercontent.com/snakers4/silero-vad/master/files/silero_vad.onnx
```

Then restart the service.

---

## Migrating from Legacy Setup (com.citadel.voice + com.citadel.whisper)

```bash
# 1. Unload old services (these were LaunchDaemons, so they need sudo)
sudo launchctl unload /Library/LaunchDaemons/com.citadel.voice.plist
sudo launchctl unload /Library/LaunchDaemons/com.citadel.whisper.plist

# 2. Remove old plists
sudo rm /Library/LaunchDaemons/com.citadel.voice.plist
sudo rm /Library/LaunchDaemons/com.citadel.whisper.plist

# 3. Install new unified plist as a LaunchAgent (no sudo required)
cp /Users/homelab/echo/com.citadel.echo.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.citadel.echo.plist
```

---

## Updating ECHO

```bash
cd /Users/homelab/echo

# Pull latest changes
git pull origin main

# Install any new dependencies
.venv/bin/pip install -r requirements.txt

# Restart service (no sudo needed for LaunchAgents)
launchctl unload ~/Library/LaunchAgents/com.citadel.echo.plist
launchctl load   ~/Library/LaunchAgents/com.citadel.echo.plist
```
