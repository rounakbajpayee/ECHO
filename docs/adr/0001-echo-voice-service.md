# ECHO — Architecture Decision Record
> **Type:** ADR · **Project:** ECHO · **Date:** 2026-05-29
> Records every significant decision made during design and build, with full reasoning,
> alternatives considered, and why they were accepted or rejected.
> Intended audience: future self, interviewers, collaborators picking this up cold.

---

## ADR-001 — Unify Python VAD Proxy and C++ whisper-server into a single service lifecycle

**Status:** Accepted

**Context:**
Previously, the voice activity detection (VAD) service and the transcription engine (`whisper.cpp` server) ran as separate, independent processes. This made service orchestration difficult, increased the likelihood of orphaned processes, and required maintaining multiple launchd configurations.

**Decision:**
Unify the lifecycle of both engines under a single FastAPI service. The C++ `whisper-server` is managed as a background subprocess using Python's `subprocess.Popen` launched during FastAPI startup and terminated cleanly during shutdown.

**Reasoning:**
- **Simplified Lifecycle**: Only one service (ECHO) needs to be started or registered in the OS launchd.
- **Process Isolation**: The C++ server runs as a child process of FastAPI. If the parent dies, the child process is cleaned up automatically, preventing orphaned processes from locking ports.
- **Unified Configuration**: All settings (such as model path, port, threads, and timeout) are loaded from a single `config.json` file.

**Alternatives considered:**
- **Docker Compose Orchestration**: Rejected because the target deployment platform is a native macOS host (Mac Mini M4) leveraging metal acceleration, and managing native plists for multiple processes is highly complex.
- **Independent LaunchAgents**: Rejected because it requires the user to load/reload multiple agents and handles crashes or sequence-dependent startups poorly.

---

## ADR-002 — Port Allocation and localhost binding for whisper-server

**Status:** Accepted

**Context:**
The C++ `whisper-server` lacks built-in authentication or security controls. Exposing it directly to the local area network (LAN) poses a security risk.

**Decision:**
- Bind the public FastAPI endpoint to `0.0.0.0:8001` to allow LAN-wide API access from other Citadel modules (like `APRIL`).
- Bind the underlying C++ `whisper-server` strictly to localhost `127.0.0.1:8003`.

**Reasoning:**
- Hiding the Whisper port (8003) from the network prevents unauthenticated clients from invoking transcription jobs directly.
- The FastAPI proxy at port 8001 acts as the sole entry gatekeeper, enforcing token validation (`Authorization: Bearer <token>`) and timing-attack protections before forwarding requests to the model.

---

## ADR-003 — Local Speech Detection using Silero VAD ONNX

**Status:** Accepted

**Context:**
Whisper is a heavy engine. Sending silent audio (such as background hums or mouse clicks) to the transcription engine wastes valuable CPU/GPU cycles and can cause Whisper to hallucinate random text phrases.

**Decision:**
Integrate the Silero VAD (Voice Activity Detection) ONNX model into the FastAPI preprocessing pipeline. Audio files are validated for speech content locally using `onnxruntime` before being forwarded to the transcription backend.

**Reasoning:**
- **Performance**: Silero VAD inference takes a few milliseconds on a single CPU thread, whereas a Whisper transcription can take several hundred milliseconds to a second.
- **Hallucination Prevention**: If speech probability is below the threshold (`0.5`), the API immediately returns `{"text": ""}` without invoking Whisper.

---

## ADR-004 — Hallucination Blocklist Filtering

**Status:** Accepted

**Context:**
Even with VAD, Whisper models (especially smaller ones or under silent/noisy conditions) are prone to hallucinating common phrases such as "Thank you.", "Please like and subscribe.", or "YouTube channel."

**Decision:**
Implement an explicit blocklist filter on the transcription return path. If the Whisper output matches any entry in `HALLUCINATION_BLOCKLIST` exactly, it is wiped and returned as `""`.

**Reasoning:**
Simple, low-overhead string comparison on the return path prevents hallucinated filler text from contaminating the downstream assistant's memory or planning system.
