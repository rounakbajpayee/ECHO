import os
import sys
from unittest.mock import MagicMock
import pytest

# Ensure "src" is in PYTHONPATH so python can locate the modules
# We must do this at module level so `import main` succeeds during collection
src_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if src_path not in sys.path:
    sys.path.insert(0, src_path)


# ---------------------------------------------------------
# Mock ONNX Runtime (onnxruntime)
# ---------------------------------------------------------
class MockInferenceSession:
    def __init__(self, model_path, sess_options=None, providers=None):
        self.model_path = model_path
        self.providers = providers

    def get_inputs(self):
        mock_input = MagicMock()
        mock_input.name = "input"
        mock_input.shape = [1, 512]
        return [mock_input]

    def get_outputs(self):
        mock_output = MagicMock()
        mock_output.name = "output"
        return [mock_output]

    def run(self, output_names, feed_dict):
        # Default mock returns a value representing speech detected (probability = 0.9)
        mock_val = MagicMock()
        mock_val.item.return_value = 0.9
        return [mock_val, MagicMock(), MagicMock(), MagicMock()]


ort_mock = MagicMock()
ort_mock.InferenceSession = MockInferenceSession
ort_mock.SessionOptions = MagicMock

# We must do this at module level so `import main` gets the mocked library
sys.modules["onnxruntime"] = ort_mock


@pytest.fixture(autouse=True)
def mock_main_config(monkeypatch):
    import main  # noqa: E402

    # Disable subprocess spawn during tests by default in CONFIG
    monkeypatch.setitem(main.CONFIG, "spawn_whisper_server", False)
    # Point VAD model path to a dummy path that exists, or skip check
    monkeypatch.setitem(main.CONFIG, "vad_model_path", __file__)


@pytest.fixture(autouse=True)
def reset_vad(monkeypatch):
    import main  # noqa: E402

    monkeypatch.setattr(main, "_vad", None)
