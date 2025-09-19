import threading
import queue
import time
from vosk_listener import vosk_listener

# ------------------- Setup coda e stop_event -------------------
audio_queue = queue.Queue()
stop_event = threading.Event()

# ------------------- Funzione di test: invia un audio di debug -------------------
def send_dummy_audio():
    import numpy as np
    # genera 1 secondo di silenzio a 16kHz
    dummy_audio = (np.zeros(16000)).astype(np.int16).tolist()
    audio_queue.put((dummy_audio, 16000))
    print("[DEBUG] Dummy audio inviato alla coda")

# ------------------- Avvio thread Vosk -------------------
t_vosk = threading.Thread(target=vosk_listener, args=(audio_queue, stop_event), daemon=True)
t_vosk.start()
print("[MAIN] Vosk listener avviato")

# ------------------- Loop principale -------------------
try:
    print("[MAIN] Sistema in ascolto per debug. Ctrl+C per fermare.")
    send_dummy_audio()  # invia un primo audio per test
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n[MAIN] Stop richiesto, chiusura thread...")
    stop_event.set()
    t_vosk.join()
    print("[MAIN] Thread Vosk terminato. Exit.")
