import vosk
import struct
import numpy as np
from scipy.signal import resample
from queue import Queue
import json
import re
import threading

VOSK_MODEL_PATH = "/home/homeassistant/vosk-model/model/vosk-model-small-it-0.22"
DOMOTICA_JSON = "/home/katone/porcupine/threadsafe/coda2/domotica.json"  # JSON con azioni e entità

# Caricamento dizionario domotica
try:
    with open(DOMOTICA_JSON, "r", encoding="utf-8") as f:
        domotica_data = json.load(f)
        ACTIONS = domotica_data.get("azioni", [])
        ENTITIES = domotica_data.get("entita", [])
        ROOMS = domotica_data.get("stanze", [])
except Exception as e:
    print(f"[VOLK] ERRORE caricamento JSON domotica: {e}")
    ACTIONS = []
    ENTITIES = []
    ROOMS = []

def filter_domotica(text: str):
    """Filtra azioni, entità e stanze da una stringa."""
    text_lower = text.lower()
    found_actions = [a for a in ACTIONS if re.search(rf"\b{re.escape(a)}\b", text_lower)]
    found_entities = [e for e in ENTITIES if re.search(rf"\b{re.escape(e)}\b", text_lower)]
    found_rooms = [r for r in ROOMS if re.search(rf"\b{re.escape(r)}\b", text_lower)]
    return found_actions, found_entities, found_rooms

def vosk_listener(audio_queue: Queue, stop_event: threading.Event, model_path=VOSK_MODEL_PATH, ready_event=None):
    """Thread Vosk che trascrive l’audio dalla coda e filtra comandi domotici."""
    print("[VOLK] Thread partito")
    print(f"[VOLK] Caricamento modello da: {model_path}")

    try:
        model = vosk.Model(model_path)
        if ready_event:
            ready_event.set()
        print("[VOLK] Modello caricato, pronto all'ascolto")
    except Exception as e:
        print(f"[VOLK] ERRORE caricamento modello: {e}")
        return

    while not stop_event.is_set():
        try:
            audio_buffer, sample_rate = audio_queue.get(timeout=1)
        except Exception:
            continue  # niente audio disponibile

        if not audio_buffer or len(audio_buffer) == 0:
            continue

        # Nuovo recognizer per ogni registrazione (evita accumulo)
        rec = vosk.KaldiRecognizer(model, 16000)

        # Conversione in numpy int16
        audio_array = np.array(audio_buffer, dtype=np.int16)

        # Resample a 16 kHz se necessario
        if sample_rate != 16000:
            num_samples = int(len(audio_array) * 16000 / sample_rate)
            if num_samples <= 0:
                continue
            audio_array = resample(audio_array, num_samples).astype(np.int16)

        # Normalizzazione
        max_val = np.max(np.abs(audio_array))
        if max_val > 0:
            audio_array = (audio_array / max_val * 32767).astype(np.int16)

        # Conversione in bytes PCM
        try:
            pcm_bytes = struct.pack("<" + "h"*len(audio_array), *audio_array)
        except Exception as e:
            print(f"[VOLK] ERRORE packing audio: {e}")
            continue

        # Elaborazione Vosk
        try:
            print("[VOLK] Presa in carico buffer audio per analisi...")
            if rec.AcceptWaveform(pcm_bytes):
                result_json = rec.Result()
                text = json.loads(result_json).get("text", "")
                actions, entities, rooms = filter_domotica(text)
                print(f"[VOLK] Comando domotico rilevato -> JSON: {json.dumps({'text': text, 'azioni': actions, 'entita': entities, 'stanze': rooms}, ensure_ascii=False)}")
            else:
                partial_json = rec.PartialResult()
                partial_text = json.loads(partial_json).get("partial", "")
                actions, entities, rooms = filter_domotica(partial_text)
                print(f"[VOLK] Comando parziale -> JSON: {json.dumps({'text': partial_text, 'azioni': actions, 'entita': entities, 'stanze': rooms}, ensure_ascii=False)}")
        except Exception as e:
            print(f"[VOLK] ERRORE riconoscimento: {e}")
