import json
import threading
import queue
import time

from vosk_listener import vosk_listener
from porcupine_listener import porcupine_listener
from fuzzy_parser import init_fuzzy, processa_comandi, command_queue, stop_event

# ==============================
# CARICA CONFIGURAZIONE ESTERNA
# ==============================
with open("config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

# ==============================
# Coda audio e segnali di pronto
# ==============================
audio_queue = queue.Queue()
vosk_ready_event = threading.Event()  # segnala quando Vosk ha finito il caricamento

# ==============================
# Inizializza fuzzy parser
# ==============================
print("[MAIN] Inizializzazione fuzzy parser...")
init_fuzzy(CONFIG)  # <-- qui garantisce che domotica.json sia popolato

# ==============================
# Thread wrapper
# ==============================
def vosk_thread():
    vosk_listener(
        audio_queue,
        stop_event,
        CONFIG["vosk"],
        command_queue=command_queue,
        ready_event=vosk_ready_event
    )

def porcupine_thread():
    print("[MAIN] Attesa Vosk pronta...")
    vosk_ready_event.wait()
    print("[MAIN] Avvio Porcupine listener...")
    porcupine_listener(audio_queue, stop_event, CONFIG["porcupine"])

# ==============================
# Avvio thread
# ==============================
t_vosk = threading.Thread(target=vosk_thread, daemon=True)
t_porc = threading.Thread(target=porcupine_thread, daemon=True)
t_fuzzy = threading.Thread(target=processa_comandi, daemon=True)

t_vosk.start()
t_porc.start()
t_fuzzy.start()

print("[MAIN] Sistema in ascolto continuo. Ctrl+C per terminare.")

# ==============================
# Loop principale
# ==============================
try:
    while True:
        time.sleep(1)
except KeyboardInterrupt:
    print("\n[MAIN] Stop richiesto, chiusura thread...")
    stop_event.set()
    t_vosk.join()
    t_porc.join()
    t_fuzzy.join()
    print("[MAIN] Tutto terminato.")
