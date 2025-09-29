import json
import threading
import queue
import time
import requests
import os

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
t_porc = threading.Thread(target=porcupine_thread, daemon=True)
t_fuzzy = threading.Thread(target=processa_comandi, daemon=True)
ha_url = CONFIG['homeassistant']['url'].rstrip('/') + '/api/services'
ha_token = CONFIG['homeassistant']['token']
t_ha = threading.Thread(target=ha_command_consumer, args=(ha_url, ha_token), daemon=True)

t_vosk.start()
t_porc.start()
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
    t_porc.join()
    t_fuzzy.join()
    t_ha.join()
    print("[MAIN] Tutto terminato.")
