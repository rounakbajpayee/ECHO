"""
Citadel Voice Service — STT proxy (Universal VAD Edition)
OpenAI-compatible /v1/audio/transcriptions endpoint on port 8001
"""
from __future__ import annotations

import io
import os
import wave
import logging
import asyncio
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import httpx
import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BEARER_TOKEN               = os.environ.get("VOICE_BEARER_TOKEN", "")
WHISPER_BACKEND_URL        = os.environ.get("WHISPER_BACKEND_URL", "http://127.0.0.1:8003")
HEAD_TRIM_MS               = int(os.environ.get("HEAD_TRIM_MS", "50"))
BACKEND_TIMEOUT            = float(os.environ.get("BACKEND_TIMEOUT", "30.0"))
VAD_ENABLED                = os.environ.get("VAD_ENABLED", "true").lower() in ("true", "1", "yes")
VAD_THRESHOLD              = float(os.environ.get("VAD_THRESHOLD", "0.5"))
VAD_MIN_SPEECH_DURATION_MS = int(os.environ.get("VAD_MIN_SPEECH_DURATION_MS", "200"))
VAD_CHUNK_MS               = int(os.environ.get("VAD_CHUNK_MS", "96"))

_DEFAULT_VAD_MODEL = str(
    Path(__file__).parent / ".venv" / "lib" / "python3.12" / "site-packages"
    / "faster_whisper" / "assets" / "silero_vad_v6.onnx"
)
VAD_MODEL_PATH = os.environ.get("VAD_MODEL_PATH", _DEFAULT_VAD_MODEL)

HALLUCINATION_BLOCKLIST: set[str] = {
    ".", "you", "You", "Bye", "bye", "Thanks", "Thanks.",
    "Thank you", "Thank you.", "Subscribe", "subscribe",
    "Thanks for watching", "Thanks for watching.", "Thank you for watching",
    "Thank you for watching.", "Please subscribe", "Please subscribe.",
    "Please like and subscribe", "Please like and subscribe.",
}

logging.basicConfig(
    level=logging.INFO,
    format="[voice-proxy] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("voice-proxy")

# ---------------------------------------------------------------------------
# Silero VAD — Universal Wrapper
# ---------------------------------------------------------------------------

class SileroVAD:
    SR = 16000

    def __init__(self, model_path: str):
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
            self._input_names, self._output_names, self._chunk_samples
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
            
            if "sr" in self._input_names: feed["sr"] = sr_tensor
            if "h" in self._input_names: feed["h"] = h
            if "c" in self._input_names: feed["c"] = c
            if "state" in self._input_names: feed["state"] = state

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

if VAD_ENABLED:
    try:
        _vad = SileroVAD(VAD_MODEL_PATH)
    except Exception as exc:
        log.error("Failed to load Silero VAD — VAD will be disabled: %s", exc)
        VAD_ENABLED = False
else:
    log.info("VAD disabled via VAD_ENABLED=false")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Citadel Voice Proxy", version="3.1.0")
_client = httpx.AsyncClient(timeout=BACKEND_TIMEOUT)


def _check_auth(request: Request) -> None:
    if not BEARER_TOKEN:
        return
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth[7:] != BEARER_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _trim_wav_head(audio_bytes: bytes, trim_ms: int) -> bytes:
    if trim_ms <= 0:
        return audio_bytes
    try:
        with wave.open(io.BytesIO(audio_bytes)) as wf:
            n_channels = wf.getnchannels()
            sampwidth  = wf.getsampwidth()
            framerate  = wf.getframerate()
            n_frames   = wf.getnframes()
            comp_type  = wf.getcomptype()

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
            sampwidth  = wf.getsampwidth()
            framerate  = wf.getframerate()
            n_frames   = wf.getnframes()
            raw        = wf.readframes(n_frames)

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


def _run_vad_sync(audio_bytes: bytes) -> tuple[bool, float]:
    audio = _wav_to_float32(audio_bytes)
    if audio is None:
        return True, -1.0

    speech_ms = _vad.get_speech_duration_ms(
        audio,
        chunk_ms=VAD_CHUNK_MS,
        threshold=VAD_THRESHOLD,
    )
    return speech_ms >= VAD_MIN_SPEECH_DURATION_MS, speech_ms


def _is_hallucination(text: str) -> bool:
    return text.strip() in HALLUCINATION_BLOCKLIST


@app.get("/health")
async def health():
    try:
        r = await _client.get(f"{WHISPER_BACKEND_URL}/health", timeout=3.0)
        backend_ok = r.status_code == 200
    except Exception:
        backend_ok = False
    return {
        "status": "ok",
        "backend": "ok" if backend_ok else "unreachable",
        "vad": "enabled" if VAD_ENABLED else "disabled",
    }


@app.post("/v1/audio/transcriptions")
async def transcribe(
    request:     Request,
    file:        UploadFile = File(...),
    model:       str        = Form(default="whisper-1"),
    language:    str        = Form(default="en"),
    temperature: str        = Form(default="0.0"),
    prompt:      str        = Form(default=""),
):
    _check_auth(request)

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file")

    original_size = len(audio_bytes)
    audio_bytes = _trim_wav_head(audio_bytes, HEAD_TRIM_MS)

    if VAD_ENABLED and _vad is not None:
        loop = asyncio.get_event_loop()
        speech_detected, speech_ms = await loop.run_in_executor(
            _vad_executor, _run_vad_sync, audio_bytes
        )
        if not speech_detected:
            log.info("VAD: no speech (%.0fms < %dms) — returning empty", speech_ms, VAD_MIN_SPEECH_DURATION_MS)
            return JSONResponse({"text": ""})
        log.info("VAD: %.0fms speech detected — forwarding", speech_ms)

    filename = file.filename or "audio.wav"
    files_payload = {"file": (filename, audio_bytes, file.content_type or "audio/wav")}
    form_fields   = {"model": model, "language": language, "temperature": temperature}
    if prompt:
        form_fields["prompt"] = prompt

    try:
        response = await _client.post(
            f"{WHISPER_BACKEND_URL}/v1/audio/transcriptions",
            data=form_fields,
            files=files_payload,
        )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Whisper backend timeout")
    except httpx.ConnectError:
        raise HTTPException(status_code=502, detail="Whisper backend unreachable")

    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=f"Backend error: {response.text[:200]}")

    result = response.json()
    text   = result.get("text", "").strip()

    if _is_hallucination(text):
        log.info("Blocked hallucination: %r", text)
        text = ""

    return JSONResponse({"text": text})
