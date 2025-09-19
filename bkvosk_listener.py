import vosk
import struct
import numpy as np
from scipy.signal import resample
from queue import Queue

VOSK_MODEL_PATH = "/home/homeassistant/vosk-model/model/vosk-model-small-it-0.22"

def vosk_listener(audio_queue: Queue, stop_event, model_path=VOSK_MODEL_PATH, ready_event=None):
    """
    Thread Volk (Vosk) che ascolta l'audio dalla coda e trascrive.
    Usa PartialResult per fornire risposte rapide e Result finale per la frase completa.
    """
    print("[VOLK] Thread partito")
    print(f"[VOLK] Caricamento modello da: {model_path}")

    # Caricamento modello
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
            continue  # niente audio

        if not audio_buffer or len(audio_buffer) == 0:
            print("[VOLK] Audio vuoto, salto trascrizione")
            continue

        # Nuovo recognizer per ogni registrazione
        rec = vosk.KaldiRecognizer(model, 16000)

        # Conversione in numpy int16
        audio_array = np.array(audio_buffer, dtype=np.int16)

        # Resample a 16 kHz
        if sample_rate != 16000:
            num_samples = int(len(audio_array) * 16000 / sample_rate)
            if num_samples <= 0:
                continue
            audio_array = resample(audio_array, num_samples).astype(np.int16)

        # Normalizzazione
        max_val = np.max(np.abs(audio_array))
        if max_val > 0:
            audio_array = (audio_array / max_val * 32767).astype(np.int16)

        try:
            pcm_bytes = struct.pack("<" + "h"*len(audio_array), *audio_array)
        except Exception as e:
            print(f"[VOLK] ERRORE packing audio: {e}")
            continue

        # Elaborazione Vosk
        print("[VOLK] Inizio scansione parola...")
        try:
            if rec.AcceptWaveform(pcm_bytes):
                result = rec.Result()
                print("[VOLK] Risultato finale:", result)
            else:
                partial = rec.PartialResult()
                print("[VOLK] Risultato parziale:", partial)
        except Exception as e:
            print(f"[VOLK] ERRORE riconoscimento: {e}")
