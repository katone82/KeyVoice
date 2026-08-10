#!/bin/bash

source "keyvoiceenv/bin/activate"

MODEL="$VIRTUAL_ENV/lib/python3.11/site-packages/openwakeword/resources/models/hey_jarvis_v0.1.onnx"

if [ ! -f "$MODEL" ]; then
    echo "[KeyVoice] Download modelli openWakeWord..."

    python -c \
      "from openwakeword.utils import download_models; download_models()"
fi

echo "[KeyVoice] Ambiente pronto."
