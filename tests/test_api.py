import io
import os
import wave
from unittest.mock import MagicMock, patch

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

import main
from main import (
    SileroVAD,
    _is_hallucination,
    _run_vad_sync,
    _trim_wav_head,
    _wav_to_float32,
    app,
    ensure_vad_model,
    init_vad,
    lifespan,
    load_config,
)

client = TestClient(app)


def create_dummy_wav(duration_sec: float = 1.0, sample_rate: int = 16000, sampwidth: int = 2, n_channels: int = 1) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(sample_rate)
        t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
        if sampwidth == 2:
            data = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
        else:
            data = (np.sin(2 * np.pi * 440 * t) * 1000000000).astype(np.int32)
        if n_channels == 2:
            data = np.repeat(data, 2)
        wf.writeframes(data.tobytes())
    return out.getvalue()


def test_load_config_env_vars():
    with patch.dict(
        os.environ,
        {
            "VAD_ENABLED": "true",
            "HEAD_TRIM_MS": "100",
            "VAD_THRESHOLD": "0.7",
            "VOICE_BEARER_TOKEN": "legacy-token",
            "WHISPER_BACKEND_URL": "http://legacy",
        },
        clear=True,
    ):
        config = load_config()
        assert config.get("vad_enabled") is True
        assert config.get("head_trim_ms") == 100
        assert config.get("vad_threshold") == 0.7
        assert config.get("bearer_token") == "legacy-token"
        assert config.get("whisper_backend_url") == "http://legacy"


def test_load_config_exceptions():
    with patch("builtins.open", side_effect=Exception("mocked err")):
        config = load_config()
        assert isinstance(config, dict)


def test_ensure_vad_model_downloads():
    with (
        patch("main.Path.exists", return_value=False),
        patch("main.Path.write_bytes") as mock_write,
        patch("httpx.Client.get") as mock_get,
    ):
        mock_resp = MagicMock()
        mock_resp.content = b"fake-onnx-data"
        mock_get.return_value = mock_resp

        ensure_vad_model("some/path/silero.onnx")
        mock_write.assert_called_once_with(b"fake-onnx-data")


def test_trim_wav_head():
    audio = create_dummy_wav(duration_sec=0.5, sample_rate=16000)
    trimmed = _trim_wav_head(audio, trim_ms=100)
    assert len(trimmed) < len(audio)

    assert _trim_wav_head(audio, 0) == audio
    assert _trim_wav_head(audio, 1000) == audio
    assert _trim_wav_head(b"not-a-wav", 100) == b"not-a-wav"


def test_wav_to_float32():
    audio = create_dummy_wav(duration_sec=0.2, sample_rate=16000)
    assert _wav_to_float32(audio).dtype == np.float32

    audio32 = create_dummy_wav(duration_sec=0.2, sampwidth=4)
    assert _wav_to_float32(audio32).dtype == np.float32

    audio_stereo = create_dummy_wav(duration_sec=0.2, n_channels=2)
    assert _wav_to_float32(audio_stereo).dtype == np.float32

    audio_44 = create_dummy_wav(duration_sec=0.2, sample_rate=44100)
    assert _wav_to_float32(audio_44) is None

    assert _wav_to_float32(b"invalid") is None


@pytest.mark.anyio
async def test_health_check():
    with patch("main._client.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_get.return_value = mock_resp
        resp = client.get("/health")
        assert resp.status_code == 200

        mock_get.side_effect = Exception("fail")
        resp2 = client.get("/health")
        assert resp2.status_code == 200
        assert resp2.json()["backend"] == "unreachable"


def test_transcribe_exceptions():
    main.CONFIG["bearer_token"] = ""
    audio = create_dummy_wav()

    with patch("main._client.post") as mock_post:
        mock_post.side_effect = httpx.TimeoutException("timeout")
        resp = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", audio, "audio/wav")})
        assert resp.status_code == 504

        mock_post.side_effect = httpx.ConnectError("connect")
        resp2 = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", audio, "audio/wav")})
        assert resp2.status_code == 502

        mock_post.side_effect = None
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_post.return_value = mock_resp
        resp3 = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", audio, "audio/wav")})
        assert resp3.status_code == 500

        resp4 = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"", "audio/wav")})
        assert resp4.status_code == 400


@pytest.mark.anyio
async def test_lifespan():
    with patch("subprocess.Popen") as mock_popen, patch("main.Path.exists", return_value=True):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc
        async with lifespan(app):
            pass
        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called()


def test_silero_vad_mocked():
    with (
        patch("main.ensure_vad_model"),
        patch("main.Path.exists", return_value=True),
        patch("onnxruntime.InferenceSession") as mock_ort,
    ):
        mock_sess = MagicMock()
        inp = MagicMock()
        inp.name = "input"
        inp.shape = [1, 512]
        out_prob = MagicMock()
        out_prob.name = "output"
        out_hn = MagicMock()
        out_hn.name = "hn"
        mock_sess.get_inputs.return_value = [inp]
        mock_sess.get_outputs.return_value = [out_prob, out_hn]

        mock_prob_tensor = MagicMock()
        mock_prob_tensor.item.return_value = 0.8
        mock_sess.run.return_value = [mock_prob_tensor, np.zeros((2, 1, 64))]
        mock_ort.return_value = mock_sess

        vad = SileroVAD("dummy.onnx")
        audio = np.zeros(1024, dtype=np.float32)
        dur = vad.get_speech_duration_ms(audio, threshold=0.5)
        assert dur > 0


def test_init_vad_and_run_sync():
    main.CONFIG["vad_enabled"] = True
    with (
        patch("main.ensure_vad_model"),
        patch("main.Path.exists", return_value=True),
        patch("onnxruntime.InferenceSession") as mock_ort,
    ):
        mock_sess = MagicMock()
        inp = MagicMock()
        inp.name = "input"
        inp.shape = [1, 512]
        out_prob = MagicMock()
        out_prob.name = "output"
        mock_sess.get_inputs.return_value = [inp]
        mock_sess.get_outputs.return_value = [out_prob]

        mock_prob_tensor = MagicMock()
        mock_prob_tensor.item.return_value = 0.8
        mock_sess.run.return_value = [mock_prob_tensor]
        mock_ort.return_value = mock_sess

        init_vad(main.CONFIG)
        assert main._vad is not None

        audio_bytes = create_dummy_wav(0.1)
        detected, dur = _run_vad_sync(audio_bytes, 96, 0.5, 10)
        assert detected is True


def test_transcribe_vad_path():
    main.CONFIG["bearer_token"] = ""
    main.CONFIG["vad_enabled"] = True

    with (
        patch("main.ensure_vad_model"),
        patch("main.Path.exists", return_value=True),
        patch("onnxruntime.InferenceSession") as mock_ort,
        patch("main._client.post") as mock_post,
    ):
        mock_sess = MagicMock()
        inp = MagicMock()
        inp.name = "input"
        inp.shape = [1, 512]
        out_prob = MagicMock()
        out_prob.name = "output"
        mock_sess.get_inputs.return_value = [inp]
        mock_sess.get_outputs.return_value = [out_prob]

        mock_prob_tensor = MagicMock()
        mock_prob_tensor.item.return_value = 0.9
        mock_sess.run.return_value = [mock_prob_tensor]
        mock_ort.return_value = mock_sess

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"text": "hello"}
        mock_post.return_value = mock_response

        init_vad(main.CONFIG)
        audio = create_dummy_wav(0.5)

        resp = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", audio, "audio/wav")})
        assert resp.status_code == 200
        assert resp.json()["text"] == "hello"

        mock_response.json.return_value = {"text": "Thanks for watching."}
        resp = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", audio, "audio/wav")})
        assert resp.json()["text"] == ""

        mock_prob_tensor.item.return_value = 0.1
        resp = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", audio, "audio/wav")})
        assert resp.json()["text"] == ""


def test_hallucination_blocklist():
    assert _is_hallucination("Thanks for watching.")
    assert _is_hallucination("Subscribe")
    assert not _is_hallucination("Hello world")
