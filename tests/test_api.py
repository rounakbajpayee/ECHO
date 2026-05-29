import io
import wave
import numpy as np
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

import main
from main import app, _trim_wav_head, _wav_to_float32

# Create TestClient
client = TestClient(app)


def create_dummy_wav(duration_sec: float = 1.0, sample_rate: int = 16000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(sample_rate)
        t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
        data = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)  # non-silent sine wave
        wf.writeframes(data.tobytes())
    return out.getvalue()


# ---------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------


def test_trim_wav_head():
    audio = create_dummy_wav(duration_sec=0.5, sample_rate=16000)
    trimmed = _trim_wav_head(audio, trim_ms=100)
    assert len(trimmed) < len(audio)
    assert len(trimmed) > 0

    # Large trim that would swallow all audio should fallback to original
    too_large = _trim_wav_head(audio, trim_ms=1000)
    assert too_large == audio


def test_wav_to_float32():
    audio = create_dummy_wav(duration_sec=0.2, sample_rate=16000)
    float32_array = _wav_to_float32(audio)
    assert float32_array is not None
    assert float32_array.dtype == np.float32
    assert len(float32_array) == 16000 * 0.2


# ---------------------------------------------------------
# API Tests
# ---------------------------------------------------------


@pytest.mark.anyio
async def test_health_check():
    # Test health check endpoint with mocked backend call
    with patch("main._client.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["backend"] == "ok"


def test_unauthorized_access():
    # Set bearer token
    main.CONFIG["bearer_token"] = "secure-token"
    audio = create_dummy_wav()

    response = client.post(
        "/v1/audio/transcriptions", files={"file": ("audio.wav", audio, "audio/wav")}, data={"model": "whisper-1"}
    )
    assert response.status_code == 401

    # Send wrong token
    response = client.post(
        "/v1/audio/transcriptions",
        headers={"Authorization": "Bearer bad-token"},
        files={"file": ("audio.wav", audio, "audio/wav")},
        data={"model": "whisper-1"},
    )
    assert response.status_code == 401

    # Restore token config to empty
    main.CONFIG["bearer_token"] = ""


@pytest.mark.anyio
async def test_transcribe_vad_no_speech():
    # Force VAD model to detect NO speech (prob = 0.0)
    with patch("onnxruntime.InferenceSession.run") as mock_run:
        mock_prob = MagicMock()
        mock_prob.item.return_value = 0.05  # below VAD_THRESHOLD
        mock_run.return_value = [mock_prob, MagicMock(), MagicMock(), MagicMock()]

        # Initialize VAD with current mocked InferenceSession
        main.init_vad(main.CONFIG)

        audio = create_dummy_wav()
        response = client.post(
            "/v1/audio/transcriptions", files={"file": ("audio.wav", audio, "audio/wav")}, data={"model": "whisper-1"}
        )
        assert response.status_code == 200
        assert response.json() == {"text": ""}


@pytest.mark.anyio
async def test_transcribe_speech_forwarded():
    # Force VAD model to detect speech (prob = 0.9)
    # Mock VAD output and httpx client POST output
    with patch("onnxruntime.InferenceSession.run") as mock_run, patch("main._client.post") as mock_post:
        mock_prob = MagicMock()
        mock_prob.item.return_value = 0.9  # above VAD_THRESHOLD
        mock_run.return_value = [mock_prob, MagicMock(), MagicMock(), MagicMock()]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "hello senior dev"}
        mock_post.return_value = mock_response

        main.init_vad(main.CONFIG)

        audio = create_dummy_wav()
        response = client.post(
            "/v1/audio/transcriptions", files={"file": ("audio.wav", audio, "audio/wav")}, data={"model": "whisper-1"}
        )
        assert response.status_code == 200
        assert response.json() == {"text": "hello senior dev"}


@pytest.mark.anyio
async def test_hallucination_blocking():
    # Force VAD model to detect speech and backend to return hallucination string
    with patch("onnxruntime.InferenceSession.run") as mock_run, patch("main._client.post") as mock_post:
        mock_prob = MagicMock()
        mock_prob.item.return_value = 0.9
        mock_run.return_value = [mock_prob, MagicMock(), MagicMock(), MagicMock()]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "Thanks for watching."}
        mock_post.return_value = mock_response

        main.init_vad(main.CONFIG)

        audio = create_dummy_wav()
        response = client.post(
            "/v1/audio/transcriptions", files={"file": ("audio.wav", audio, "audio/wav")}, data={"model": "whisper-1"}
        )
        assert response.status_code == 200
        assert response.json() == {"text": ""}
