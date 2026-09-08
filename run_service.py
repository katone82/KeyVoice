import json
import threading
import queue
import time
import requests
import os
import sys

from vosk_listener import vosk_listener
from xvf3800_wakeword_listener import WakeWordListener
from fuzzy_parser import init_fuzzy, processa_comandi, command_queue, stop_event

# ==============================
# CARICA CONFIGURAZIONE ESTERNA
# ==============================
with open("config/config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

sys.profile = CONFIG['profile']

# NOTA: la sezione CONFIG["openwakeword"] non è più usata. Il
# nuovo motore XVF3800 (xvf3800_wakeword_listener.py) non legge
# più la configurazione da config.json: usa le proprie costanti
# di modulo (WAKEWORD, WAKE_THRESHOLD_HIGH/LOW, soglie di
# direzione, ecc.) definite in cima al file. Se in futuro serve
# renderle configurabili da config.json, vanno esposte lì.

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

def wakeword_thread():
    print("[MAIN] Attesa Vosk pronta...")
    vosk_ready_event.wait()
    print(
        "[MAIN] Avvio XVF3800 wake word listener..."
    )
    # Il nuovo motore gestisce da solo wake word + cattura del
    # comando con filtro direzionale (accetta audio solo dalla
    # direzione di provenienza della wake word), e inoltra
    # l'audio catturato direttamente su audio_queue per Vosk,
    # nello stesso formato (buffer, sample_rate) che usava il
    # vecchio openwakeword_listener.py, ora dismesso.
    listener = WakeWordListener(
        vosk_audio_queue=audio_queue,
        stop_event=stop_event
    )
    listener.run()

def invia_comando_ha(cmd, ha_url, ha_token):
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json"
    }
    if cmd['azione'] in ('accendi', 'spegni') and cmd['entity_id']:
        domain = cmd['entity_id'].split('.')[0]
        service = 'turn_on' if cmd['azione'] == 'accendi' else 'turn_off'
        url = f"{ha_url}/{domain}/{service}"
        data = {"entity_id": cmd['entity_id']}
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=5)
            if resp.ok:
                print(f"[HA] Comando inviato: {cmd['azione']} {cmd['entity_id']} -> OK")
            else:
                print(f"[HA] Errore risposta: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"[HA] Errore invio comando: {e}")
    else:
        print(f"[HA] Comando non gestito: {cmd}")

def ha_command_consumer(ha_url, ha_token):
    from fuzzy_parser import ha_command_queue, stop_event
    import time
    while not stop_event.is_set():
        try:
            cmd = ha_command_queue.get(timeout=1)
        except queue.Empty:
            continue
        invia_comando_ha(cmd, ha_url, ha_token)
        time.sleep(0.1)

# ==============================
# Avvio thread
# ==============================
t_vosk = threading.Thread(target=vosk_thread, daemon=True)
t_wakeword = threading.Thread(target=wakeword_thread, daemon=True)
t_fuzzy = threading.Thread(target=processa_comandi, daemon=True)
ha_url = CONFIG['homeassistant']['url'].rstrip('/') + '/api/services'
ha_token = CONFIG['homeassistant']['token']
t_ha = threading.Thread(target=ha_command_consumer, args=(ha_url, ha_token), daemon=True)

t_vosk.start()
t_wakeword.start()
t_fuzzy.start()
t_ha.start()

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
    t_wakeword.join()
    t_fuzzy.join()
    t_ha.join()
    print("[MAIN] Tutto terminato.")