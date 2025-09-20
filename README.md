# mywakeword


{
  "porcupine": {
    "access_key": "INSERISCI_TUA_API_KEY_PORCUPINE",
    "keywords": ["jarvis"],
    "sensitivity": 0.7,
    "pre_buffer_seconds": 0.5,
    "wakeword_trim_ms": 300,
    "silence_threshold": 500,
    "silence_duration": 2
  },
  "vosk": {
    "model_path": "models/vosk-model-small-it-0.22",
    "sample_rate": 16000
  }
}
