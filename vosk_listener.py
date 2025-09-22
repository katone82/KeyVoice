import vosk
import struct
import numpy as np
from scipy.signal import resample_poly
from queue import Queue
import json
import threading

def vosk_listener(audio_queue: Queue, stop_event: threading.Event, config: dict, command_queue: Queue = None, ready_event=None):
    import time
    """
    Thread Vosk: trascrive l’audio dalla coda e invia la frase semplice al fuzzy parser.
    Non fa alcun check sul comando, solo trascrizione.
    """
    model_path = config["model_path"]

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

    rec = vosk.KaldiRecognizer(model, 16000)
    while not stop_event.is_set():
        try:
            audio_buffer, sample_rate = audio_queue.get(timeout=1)
        except Exception:
            continue

        if not audio_buffer:
            continue

        audio_array = np.array(audio_buffer, dtype=np.int16)

        # Resample a 16 kHz se necessario
        if sample_rate != 16000:
            num_samples = int(len(audio_array) * 16000 / sample_rate)
            if num_samples <= 0:
                continue
            audio_array = resample_poly(audio_array, 16000, sample_rate).astype(np.int16)

        # Normalizzazione
        max_val = np.max(np.abs(audio_array))
        if max_val > 0:
            audio_array = (audio_array / max_val * 32767).astype(np.int16)

        # Conversione in bytes PCM
        try:
            pcm_bytes = audio_array.tobytes()
        except Exception as e:
            print(f"[VOLK] ERRORE packing audio: {e}")
            continue

        # Elaborazione Vosk
        try:
            start_time = time.time()
            if rec.AcceptWaveform(pcm_bytes):
                result_json = rec.Result()
                text = json.loads(result_json).get("text", "")
                elapsed = time.time() - start_time
                if text.strip():
                    print(f"[VOLK] Comando finale trascritto: {text} [Vosk decode time: {elapsed:.3f}s]")
                    if command_queue:
                        command_queue.put(text)
            else:
                partial_json = rec.PartialResult()
                partial_text = json.loads(partial_json).get("partial", "")
                elapsed = time.time() - start_time
                if partial_text.strip():
                    print(f"[VOLK] Comando parziale trascritto: {partial_text} - time: {elapsed:.3f}s]")
                    if command_queue:
                        command_queue.put(partial_text)
        except Exception as e:
            print(f"[VOLK] ERRORE riconoscimento: {e}")
