"""
ECHO Voice Service — STT proxy (Universal VAD Edition)
OpenAI-compatible /v1/audio/transcriptions endpoint on port 8001
"""

from __future__ import annotations

import io
import os
import wave
import hmac
import logging
import asyncio
import json
import subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
from contextlib import asynccontextmanager

import httpx
import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry import propagate

# ---------------------------------------------------------------------------
# Telemetry Setup
# ---------------------------------------------------------------------------

tracer_provider = TracerProvider()
trace.set_tracer_provider(tracer_provider)

otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
if otlp_endpoint:
    exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
else:
    exporter = ConsoleSpanExporter()

tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

# ---------------------------------------------------------------------------
# Logging Setup
# ---------------------------------------------------------------------------

class OpenTelemetryFilter(logging.Filter):
    def filter(self, record):
        span = trace.get_current_span()
        if span and span.is_recording():
            span_context = span.get_span_context()
            record.trace_id = trace.format_trace_id(span_context.trace_id)
            record.span_id = trace.format_span_id(span_context.span_id)
        else:
            record.trace_id = ""
            record.span_id = ""
        return True

logging.basicConfig(
    level=logging.INFO,
    format="[echo-stt] %(asctime)s %(levelname)s trace_id=%(trace_id)s span_id=%(span_id)s %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger()
for handler in logger.handlers:
    handler.addFilter(OpenTelemetryFilter())

log = logging.getLogger("echo-stt")
log.addFilter(OpenTelemetryFilter())

# ---------------------------------------------------------------------------
# Config Loader
# ---------------------------------------------------------------------------


def load_config() -> dict:
    BASE_DIR = Path(__file__).resolve().parent
    CONFIG_PATH = BASE_DIR / "config.json"
    DEFAULT_PATH = BASE_DIR / "config_defaults.json"

    config = {}
    # 1. Load defaults
    if DEFAULT_PATH.exists():
        try:
            with open(DEFAULT_PATH, encoding="utf-8") as f:
                config.update(json.load(f))
        except Exception as exc:
            log.warning("Failed to load config_defaults.json: %s", exc)

    # 2. Overwrite with local config.json
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                config.update(json.load(f))
        except Exception as exc:
            log.warning("Failed to load config.json: %s", exc)

    # 3. Override with environment variables
    for key in list(config.keys()):
        env_val = os.environ.get(key.upper())
        if env_val is not None:
            expected_type = type(config[key])
            try:
                if expected_type is bool:
                    config[key] = env_val.lower() in ("true", "1", "yes")
                elif expected_type is int:
                    config[key] = int(env_val)
                elif expected_type is float:
                    config[key] = float(env_val)
                else:
                    config[key] = env_val
            except Exception as exc:
                log.warning("Failed to convert env var %s to %s: %s", key.upper(), expected_type, exc)

    # Handle legacy environment variable overrides
    legacy_token = os.environ.get("VOICE_BEARER_TOKEN")
    if legacy_token:
        config["bearer_token"] = legacy_token

    legacy_backend = os.environ.get("WHISPER_BACKEND_URL")
    if legacy_backend:
        config["whisper_backend_url"] = legacy_backend

    return config


CONFIG = load_config()
_whisper_subproc: Optional[subprocess.Popen] = None

# ---------------------------------------------------------------------------
# VAD Auto-Downloader
# ---------------------------------------------------------------------------


def ensure_vad_model(model_path: str):
    path = Path(model_path)
    if path.exists():
        return
    log.info("Silero VAD model not found at %s. Attempting to download...", model_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    url = "https://raw.githubusercontent.com/snakers4/silero-vad/master/src/silero_vad/data/silero_vad.onnx"
    try:
        with httpx.Client(follow_redirects=True) as client:
            r = client.get(url, timeout=30.0)
            r.raise_for_status()
            path.write_bytes(r.content)
        log.info("Silero VAD model downloaded successfully to %s", model_path)
    except Exception as exc:
        log.error("Failed to download Silero VAD model: %s. VAD will likely fail to load.", exc)


# ---------------------------------------------------------------------------
# Silero VAD — Universal Wrapper
# ---------------------------------------------------------------------------


class SileroVAD:
    SR = 16000

    def __init__(self, model_path: str):
        ensure_vad_model(model_path)
        if not Path(model_path).exists():
            raise FileNotFoundError(f"Silero VAD model not found: {model_path}")

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 3

        self._session = ort.InferenceSession(
            model_path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self._input_names = [i.name for i in self._session.get_inputs()]
        self._output_names = [o.name for o in self._session.get_outputs()]

        try:
            self._chunk_samples = self._session.get_inputs()[0].shape[1]
            if not isinstance(self._chunk_samples, int) or self._chunk_samples <= 0:
                self._chunk_samples = 512
        except Exception:
            self._chunk_samples = 512

        log.info(
            "Silero VAD loaded. Inputs: %s, Outputs: %s, Chunk Samples: %d",
            self._input_names,
            self._output_names,
            self._chunk_samples,
        )

    def get_speech_duration_ms(
        self,
        audio_float32: np.ndarray,
        chunk_ms: int = 96,
        threshold: float = 0.5,
    ) -> float:
        chunk_samples = self._chunk_samples
        actual_chunk_ms = (chunk_samples / self.SR) * 1000.0

        h = np.zeros((2, 1, 64), dtype=np.float32)
        c = np.zeros((2, 1, 64), dtype=np.float32)
        state = np.zeros((2, 1, 128), dtype=np.float32)
        sr_tensor = np.array(self.SR, dtype=np.int64)

        speech_ms = 0.0
        pad = (-len(audio_float32)) % chunk_samples
        if pad:
            audio_float32 = np.concatenate([audio_float32, np.zeros(pad, dtype=np.float32)])

        for i in range(0, len(audio_float32), chunk_samples):
            chunk = audio_float32[i : i + chunk_samples].reshape(1, -1)
            feed = {"input": chunk}

            if "sr" in self._input_names:
                feed["sr"] = sr_tensor
            if "h" in self._input_names:
                feed["h"] = h
            if "c" in self._input_names:
                feed["c"] = c
            if "state" in self._input_names:
                feed["state"] = state

            outputs = self._session.run(self._output_names, feed)
            prob = outputs[0].item()

            if "hn" in self._output_names:
                h = outputs[self._output_names.index("hn")]
            if "cn" in self._output_names:
                c = outputs[self._output_names.index("cn")]
            if "stateN" in self._output_names:
                state = outputs[self._output_names.index("stateN")]

            if prob >= threshold:
                speech_ms += actual_chunk_ms

        return speech_ms


_vad: Optional[SileroVAD] = None
_vad_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vad")


def init_vad(config: dict) -> None:
    global _vad
    if bool(config.get("vad_enabled", True)):
        try:
            model_path = str(config.get("vad_model_path", "src/assets/silero_vad_v6.onnx"))
            _vad = SileroVAD(model_path)
        except Exception as exc:
            log.error("Failed to load Silero VAD — VAD will be disabled: %s", exc)
            config["vad_enabled"] = False
    else:
        log.info("VAD disabled via config setting")


# ---------------------------------------------------------------------------
# Lifespan Context Manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup Setup
    global _whisper_subproc, CONFIG
    CONFIG = load_config()
    init_vad(CONFIG)

    # Check if we should spawn the C++ whisper-server
    spawn_server = bool(CONFIG.get("spawn_whisper_server", True))
    server_path = str(CONFIG.get("whisper_server_path", "")).strip()
    model_path = str(CONFIG.get("whisper_model_path", "")).strip()
    port = int(CONFIG.get("whisper_port", 8003))
    threads = int(CONFIG.get("whisper_threads", 4))
    extra_args = list(CONFIG.get("whisper_args", []))

    if spawn_server and server_path and model_path:
        log.info("Spawning whisper-server: %s --model %s --host 127.0.0.1 --port %d", server_path, model_path, port)
        if not Path(server_path).exists():
            log.warning(
                "whisper-server binary not found at: %s. Assuming it is in system PATH or skipping launch.", server_path
            )

        cmd = [
            server_path,
            "--model",
            model_path,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--threads",
            str(threads),
            "--inference-path",
            "/v1/audio/transcriptions",
            "--convert",
        ]
        cmd.extend(extra_args)

        try:
            creationflags = 0
            if os.name == "nt":
                creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            _whisper_subproc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
            log.info("whisper-server spawned successfully with PID %d", _whisper_subproc.pid)
            await asyncio.sleep(1.0)
        except Exception as exc:
            log.error("Failed to spawn whisper-server: %s", exc)

    yield

    # Shutdown Cleanup
    if _whisper_subproc is not None:
        log.info("Terminating whisper-server subprocess...")
        _whisper_subproc.terminate()
        try:
            _whisper_subproc.wait(timeout=5.0)
            log.info("whisper-server terminated cleanly.")
        except subprocess.TimeoutExpired:
            log.warning("whisper-server did not exit within timeout, killing...")
            _whisper_subproc.kill()
            _whisper_subproc.wait()
            log.info("whisper-server killed.")


# ---------------------------------------------------------------------------
# App Instance
# ---------------------------------------------------------------------------

app = FastAPI(title="ECHO Voice Service", version="4.0.0", lifespan=lifespan)
_client = httpx.AsyncClient(timeout=float(CONFIG.get("backend_timeout", 30.0)))

# ---------------------------------------------------------------------------
# Business Logic Helpers
# ---------------------------------------------------------------------------


def _check_auth(request: Request, bearer_token: str) -> None:
    if not bearer_token:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not hmac.compare_digest(auth[7:], bearer_token):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _trim_wav_head(audio_bytes: bytes, trim_ms: int) -> bytes:
    if trim_ms <= 0:
        return audio_bytes
    try:
        with wave.open(io.BytesIO(audio_bytes)) as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            n_frames = wf.getnframes()
            comp_type = wf.getcomptype()

        if comp_type != "NONE":
            log.warning("Non-PCM WAV (comptype=%s), skipping head trim", comp_type)
            return audio_bytes

        trim_frames = int(framerate * trim_ms / 1000)
        if trim_frames >= n_frames:
            log.warning("Head trim (%dms) would consume all audio, skipping", trim_ms)
            return audio_bytes

        with wave.open(io.BytesIO(audio_bytes)) as wf:
            wf.readframes(trim_frames)
            remaining = wf.readframes(n_frames - trim_frames)

        out = io.BytesIO()
        with wave.open(out, "wb") as wf_out:
            wf_out.setnchannels(n_channels)
            wf_out.setsampwidth(sampwidth)
            wf_out.setframerate(framerate)
            wf_out.setcomptype("NONE", "not compressed")
            wf_out.writeframes(remaining)

        result = out.getvalue()
        log.info("Head trim: removed %dms, %d→%d bytes", trim_ms, len(audio_bytes), len(result))
        return result
    except Exception as exc:
        log.warning("Head trim failed (%s), using original", exc)
        return audio_bytes


def _wav_to_float32(audio_bytes: bytes) -> Optional[np.ndarray]:
    try:
        with wave.open(io.BytesIO(audio_bytes)) as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)

        if framerate != SileroVAD.SR:
            log.warning("Audio is %dHz, VAD requires 16kHz — skipping VAD", framerate)
            return None

        if sampwidth == 2:
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        elif sampwidth == 4:
            audio = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
        else:
            log.warning("Unsupported sample width %d, skipping VAD", sampwidth)
            return None

        if n_channels > 1:
            audio = audio.reshape(-1, n_channels).mean(axis=1)

        # peak normalization for quiet microphones
        if len(audio) > 0:
            max_val = np.max(np.abs(audio))
            if max_val > 0.01:
                audio = audio / max_val

        return audio
    except Exception as exc:
        log.warning("WAV decode failed (%s), skipping VAD", exc)
        return None


def _run_vad_sync(audio_bytes: bytes, chunk_ms: int, threshold: float, min_duration: int) -> tuple[bool, float]:
    audio = _wav_to_float32(audio_bytes)
    if audio is None:
        return True, -1.0

    speech_ms = _vad.get_speech_duration_ms(
        audio,
        chunk_ms=chunk_ms,
        threshold=threshold,
    )
    return speech_ms >= min_duration, speech_ms


HALLUCINATION_BLOCKLIST: set[str] = {
    ".",
    "you",
    "You",
    "Bye",
    "bye",
    "Thanks",
    "Thanks.",
    "Thank you",
    "Thank you.",
    "Subscribe",
    "subscribe",
    "Thanks for watching",
    "Thanks for watching.",
    "Thank you for watching",
    "Thank you for watching.",
    "Please subscribe",
    "Please subscribe.",
    "Please like and subscribe",
    "Please like and subscribe.",
}


def _is_hallucination(text: str) -> bool:
    return text.strip() in HALLUCINATION_BLOCKLIST


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    backend_url = str(CONFIG.get("whisper_backend_url", "http://127.0.0.1:8003"))
    try:
        r = await _client.get(f"{backend_url}/health", timeout=3.0)
        backend_ok = r.status_code == 200
    except Exception:
        backend_ok = False
    return {
        "status": "ok",
        "backend": "ok" if backend_ok else "unreachable",
        "vad": "enabled" if bool(CONFIG.get("vad_enabled", True)) else "disabled",
    }


@app.post("/v1/audio/transcriptions")
async def transcribe(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form(default="whisper-1"),
    language: str = Form(default="en"),
    temperature: str = Form(default="0.0"),
    prompt: str = Form(default=""),
):
    bearer_token = str(CONFIG.get("bearer_token", "") or "").strip()
    _check_auth(request, bearer_token)

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file")

    head_trim_ms = int(CONFIG.get("head_trim_ms", 50))
    audio_bytes = _trim_wav_head(audio_bytes, head_trim_ms)

    vad_enabled = bool(CONFIG.get("vad_enabled", True))
    if vad_enabled and _vad is not None:
        chunk_ms = int(CONFIG.get("vad_chunk_ms", 96))
        threshold = float(CONFIG.get("vad_threshold", 0.5))
        min_duration = int(CONFIG.get("vad_min_speech_duration_ms", 200))

        loop = asyncio.get_event_loop()
        speech_detected, speech_ms = await loop.run_in_executor(
            _vad_executor, _run_vad_sync, audio_bytes, chunk_ms, threshold, min_duration
        )
        if not speech_detected:
            log.info("VAD: no speech (%.0fms < %dms) — returning empty", speech_ms, min_duration)
            return JSONResponse({"text": ""})
        log.info("VAD: %.0fms speech detected — forwarding", speech_ms)

    filename = file.filename or "audio.wav"
    files_payload = {"file": (filename, audio_bytes, file.content_type or "audio/wav")}
    form_fields = {"model": model, "language": language, "temperature": temperature}
    if prompt:
        form_fields["prompt"] = prompt

    backend_url = str(CONFIG.get("whisper_backend_url", "http://127.0.0.1:8003"))

    headers = {}
    propagate.inject(headers)

    try:
        response = await _client.post(
            f"{backend_url}/v1/audio/transcriptions",
            data=form_fields,
            files=files_payload,
            headers=headers,
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Whisper backend timeout")
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Whisper backend unreachable")

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=f"Backend error: {response.text[:200]}")

    result = response.json()
    text = result.get("text", "").strip()

    if _is_hallucination(text):
        log.info("Blocked hallucination: %r", text)
        text = ""

    return JSONResponse({"text": text})
