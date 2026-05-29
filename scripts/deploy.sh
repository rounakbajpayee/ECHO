#!/bin/bash
# deploy.sh — Native macOS LaunchAgent deployment script (No Sudo Required)

set -e # Exit immediately on command error

echo "========================================="
echo "Starting User LaunchAgent Deploy for ECHO"
echo "========================================="

# Ensure running on macOS
if [[ "$OSTYPE" != "darwin"* ]]; then
    echo "ERROR: This script must run on a macOS host."
    exit 1
fi

# Define paths
ECHO_ROOT="/Users/homelab/echo"
REPO_DIR="$ECHO_ROOT"
VENV_DIR="$ECHO_ROOT/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
PLIST_NAME="com.citadel.echo.plist"
PLIST_SOURCE="$REPO_DIR/$PLIST_NAME"
PLIST_TARGET="/Users/homelab/Library/LaunchAgents/$PLIST_NAME"

cd "$REPO_DIR"

# 1. Capture the previous commit SHA for rollback
PREV_COMMIT=$(git rev-parse HEAD)
echo "Current commit SHA: $PREV_COMMIT"

# 2. Fetch and pull latest changes from main
echo "Pulling latest code from origin/main..."
git fetch origin
git reset --hard origin/main
NEW_COMMIT=$(git rev-parse HEAD)
echo "New commit SHA: $NEW_COMMIT"

# Rollback function in case of deploy/test failure
rollback() {
    echo "========================================="
    echo "WARNING: Deployment failed! Initiating rollback..."
    echo "========================================="
    cd "$REPO_DIR"
    git reset --hard "$PREV_COMMIT"

    # Re-run setup and restart on previous commit
    setup_env
    restart_service

    echo "Rollback successful. Restored commit $PREV_COMMIT."
    exit 1
}

# 3. Setup / Update Python virtual environment
setup_env() {
    echo "Configuring Python virtual environment..."
    if [ -d "$VENV_DIR" ]; then
        # Check if the python interpreter works and if uvicorn runs without shebang errors (e.g. from venv relocation)
        if ! "$VENV_PYTHON" -c "import sys" >/dev/null 2>&1 || ! "$VENV_DIR/bin/uvicorn" --version >/dev/null 2>&1; then
            echo "Virtual environment or uvicorn interpreter is broken (likely due to relocation or updates). Recreating..."
            rm -rf "$VENV_DIR"
        fi
    fi

    if [ ! -d "$VENV_DIR" ]; then
        echo "Creating virtual environment at $VENV_DIR..."
        /opt/homebrew/bin/python3.12 -m venv "$VENV_DIR"
    fi

    # Use the virtual environment's python directly to avoid PATH activation issues in non-interactive SSH

    echo "Installing requirements..."
    "$VENV_PYTHON" -m pip install -U pip setuptools
    "$VENV_PYTHON" -m pip install -r requirements.txt

    if [ -f "requirements-dev.txt" ]; then
        echo "Installing dev requirements for host testing..."
        "$VENV_PYTHON" -m pip install -r requirements-dev.txt
    fi
}

# 4. Clean up old services and plists (obsolete voice/whisper plists)
cleanup_old_services() {
    echo "Cleaning up obsolete plists and services..."
    USER_ID=$(id -u)

    # 1. Unload and delete old com.citadel.voice LaunchAgent
    VOICE_TARGET="/Users/homelab/Library/LaunchAgents/com.citadel.voice.plist"
    if launchctl list | grep -q "com.citadel.voice"; then
        echo "Stopping obsolete com.citadel.voice service..."
        launchctl bootout gui/"$USER_ID" "$VOICE_TARGET" || launchctl bootout gui/"$USER_ID"/com.citadel.voice || true
    fi
    if [ -f "$VOICE_TARGET" ]; then
        echo "Removing obsolete com.citadel.voice.plist from LaunchAgents..."
        rm -f "$VOICE_TARGET"
    fi

    # 2. Unload and delete old com.citadel.whisper LaunchAgent
    WHISPER_TARGET="/Users/homelab/Library/LaunchAgents/com.citadel.whisper.plist"
    if launchctl list | grep -q "com.citadel.whisper"; then
        echo "Stopping obsolete com.citadel.whisper service..."
        launchctl bootout gui/"$USER_ID" "$WHISPER_TARGET" || launchctl bootout gui/"$USER_ID"/com.citadel.whisper || true
    fi
    if [ -f "$WHISPER_TARGET" ]; then
        echo "Removing obsolete com.citadel.whisper.plist from LaunchAgents..."
        rm -f "$WHISPER_TARGET"
    fi

    # 3. Clean up obsolete system-level LaunchDaemons from the repository if they were copied on host
    rm -f "$REPO_DIR/com.citadel.voice.plist"
    rm -f "$REPO_DIR/com.citadel.whisper.plist"

    # 4. Copy VAD model locally from old voice directory if available
    OLD_VAD="/Users/homelab/voice/src/assets/silero_vad_v6.onnx"
    NEW_VAD_DIR="$REPO_DIR/src/assets"
    mkdir -p "$NEW_VAD_DIR"
    if [ ! -f "$NEW_VAD_DIR/silero_vad_v6.onnx" ] && [ -f "$OLD_VAD" ]; then
        echo "Copying local Silero VAD model from obsolete voice directory..."
        cp "$OLD_VAD" "$NEW_VAD_DIR/silero_vad_v6.onnx"
    fi
}

# 5. Install LaunchAgent plist
install_plist() {
    echo "Checking LaunchAgent configuration..."

    # Ensure target directory exists
    mkdir -p "/Users/homelab/Library/LaunchAgents"

    if [ -f "$PLIST_SOURCE" ]; then
        # Install or update plist only if it changed
        if [ ! -f "$PLIST_TARGET" ] || ! cmp -s "$PLIST_SOURCE" "$PLIST_TARGET"; then
            echo "Installing/Updating LaunchAgent plist..."
            cp "$PLIST_SOURCE" "$PLIST_TARGET"
            chmod 644 "$PLIST_TARGET"
            echo "LaunchAgent plist updated."
        else
            echo "LaunchAgent plist is already up to date."
        fi
    else
        echo "ERROR: Plist file $PLIST_SOURCE not found in repo."
        exit 1
    fi
}

# 5. Restart LaunchAgent service
restart_service() {
    echo "Restarting com.citadel.echo service via launchctl..."
    USER_ID=$(id -u)

    # Check if service is currently loaded
    if launchctl list | grep -q "com.citadel.echo"; then
        echo "Stopping active service..."
        launchctl bootout gui/"$USER_ID" "$PLIST_TARGET" || launchctl bootout gui/"$USER_ID"/com.citadel.echo || true
        sleep 1
    fi

    echo "Bootstrapping service..."
    launchctl bootstrap gui/"$USER_ID" "$PLIST_TARGET"
    sleep 3 # Allow service startup time
}

# Run deployment steps
trap 'rollback' ERR # Trap errors to trigger rollback

setup_env
cleanup_old_services
install_plist
restart_service

# 6. Post-deploy health check
echo "Running post-deployment health check..."
HEALTH=$(curl -sf http://127.0.0.1:8001/health)
STATUS=$(echo "$HEALTH" | "$VENV_PYTHON" -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))")
if [ "$STATUS" != "ok" ]; then
    echo "ERROR: Health check failed — response: $HEALTH"
    false # Trigger ERR trap → rollback
fi
echo "Health check passed: $HEALTH"

echo "========================================="
echo "Deployment completed successfully!"
echo "========================================="
