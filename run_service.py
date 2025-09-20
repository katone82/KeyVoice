import json
import threading
import queue
import time
import signal

from vosk_listener import vosk_listener
from porcupine_listener import porcupine_listener

# ==============================
# CARICA CONFIGURAZIONE ESTERNA
# ==============================
with open("config.json", "r") as f:
    CONFIG = json.load(f)
# ==============================

# Coda e segnali di stop
audio_queue = queue.Queue()
stop_event = threading.Event()
volk_ready_event = threading.Event()  # segnala quando Vosk ha finito il caricamento

# Thread wrapper
def volk_thread():
    vosk_listener(audio_queue, stop_event, CONFIG["vosk"], ready_event=volk_ready_event)

def porcupine_thread():
    print("[MAIN] Attesa Volk pronta...")
    volk_ready_event.wait()
    print("[MAIN] Avvio Porcupine listener...")
    porcupine_listener(audio_queue, stop_event, CONFIG["porcupine"])

# Avvio dei thread
t_volk = threading.Thread(target=volk_thread, daemon=True)
t_porc = threading.Thread(target=porcupine_thread, daemon=True)

t_volk.start()
t_porc.start()

print("[MAIN] Sistema in ascolto continuo. Ctrl+C per terminare.")

try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n[MAIN] Stop richiesto, chiusura thread...")
    stop_event.set()
    t_volk.join()
    t_porc.join()
    print("[MAIN] Tutto terminato.")

