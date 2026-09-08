
/
Claude ha terminato la risposta


Messaggio compresso
Ha pensato per 22s
Hai ragione, buona cattura — from debug_config import DEBUG_LOGGING copia il valore al momento dell'import, quindi anche se poi lo cambi dopo aver letto config.json, i moduli già importati continuano a vedere il vecchio valore. Non è "incompleto", è proprio sbagliato per un flag che deve essere impostato a runtime da config.json. Sistemo usando l'attributo di modulo (debug_config.DEBUG_LOGGING) ovunque, che invece si aggiorna correttamente.

Eseguiti 8 comandi, 5 file modificati e 2 altri strumenti
Riepilogo del fix
Il bug: from debug_config import DEBUG_LOGGING copia il valore booleano al momento dell'import (import che avvengono all'inizio di run_service.py, prima che config.json venga letto). Cambiare debug_config.DEBUG_LOGGING dopo non aveva alcun effetto sui moduli già importati.

Il fix: tutti i listener ora fanno import debug_config e leggono debug_config.DEBUG_LOGGING (accesso ad attributo, sempre "fresco") invece di importare il nome direttamente.

Aggiungi questa chiave al tuo config.json (facoltativa, default false se assente):

json
{
  "project_path": "/home/homeassistant/KeyVoice",
  "venv_path": "/home/homeassistant/KeyVoice/keyvoiceenv",
  "config_path": "/home/homeassistant/KeyVoice/config",
  "service_user": "katone",
  "debug_logging": false,
  "openwakeword": { ... },
  "vosk": { ... }
}
In run_service.py, subito dopo aver caricato CONFIG:

python
debug_config.DEBUG_LOGGING = CONFIG.get("debug_logging", False)
Questo avviene prima che qualunque thread parta, quindi tutti i listener (wake word, Vosk, fuzzy parser) lo vedono correttamente da subito. Per attivare il debug basta mettere "debug_logging": true in config.json e riavviare il servizio — niente più bisogno di editare file Python.


Debug config
PY 

Run service
PY 

Xvf3800 wakeword listener
PY 

Vosk listener
PY 

Fuzzy parser
PY 
e qui non possiamo fare che legge da config per tutti

Ha pensato per 51s
Sì, ha senso — abbiamo passato tutta questa conversazione a ritarare costanti hardcoded nel file. Le rendo configurabili da config.json, riusando la sezione "openwakeword" che hai già (aggiornandone il contenuto per il nuovo motore).

4 file modificati, ha visualizzato 3 file e 3 altri strumenti
Riepilogo
Nuova funzione apply_config(cfg) in xvf3800_wakeword_listener.py: sovrascrive le costanti di modulo (soglie wake word, tempistiche, tolleranza/grazia direzione, AGC, canale audio, beep, device microfono) con eventuali valori letti da config.json, chiave assente = resta il default già ritarato insieme in questa conversazione.

run_service.py chiama apply_config(CONFIG.get("openwakeword", {})) subito dopo aver caricato config.json, prima di avviare qualunque thread — stessa logica già usata per debug_logging.

Aggiorna la sezione "openwakeword" nel tuo config.json (sostituisce i vecchi campi, ormai riferiti al motore dismesso):

json
"openwakeword": {
  "model": "hey_jarvis",
  "threshold_high": 0.45,
  "threshold_low": 0.15,
  "confirm_chunks": 3,
  "pre_roll_seconds": 0.25,
  "post_wake_ignore_seconds": 0.40,
  "command_timeout_seconds": 4.0,
  "max_command_seconds": 6.0,
  "speech_start_ratio": 2.5,
  "speech_end_ratio": 1.5,
  "speech_end_time": 0.60,
  "direction_angle_tolerance": 60.0,
  "direction_lost_grace_seconds": 0.5,
  "direction_min_dominance": 1.2,
  "agc_max_gain": 8.0,
  "agc_silence_floor": 15,
  "wake_audio_channel": 0,
  "beep_device": "plughw:3,0",
  "beep_file": "./sounds/wake.wav",
  "input_device_name": "reSpeaker XVF3800 4-Mic Array"
}
Puoi omettere qualunque chiave: se manca, resta il valore di default già nel codice. Così d'ora in poi per ritarare (es. dopo qualche giorno d'uso) basta editare config.json e riavviare il servizio, senza toccare più il codice Python.


Run service
PY 

Xvf3800 wakeword listener
PY 

Scrivi un messaggio…


Claude è un’IA e può commettere errori. Si prega di verificare sempre le risposte.
Run service · PY
import json
import threading
import queue
import time
import requests
import os
import sys
 
import debug_config
from vosk_listener import vosk_listener
from xvf3800_wakeword_listener import WakeWordListener, apply_config
from fuzzy_parser import init_fuzzy, processa_comandi, command_queue, stop_event
 
# ==============================
# CARICA CONFIGURAZIONE ESTERNA
# ==============================
with open("config/config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)
 
sys.profile = CONFIG['profile']
 
# Flag di debug condiviso da tutto KeyVoice (wake word listener,
# Vosk, fuzzy parser), letto da config.json — chiave opzionale,
# default False se assente:
#
#   "debug_logging": true
#
# Deve essere impostato QUI, prima che i thread partano. Uso
# l'attributo di modulo (debug_config.DEBUG_LOGGING = ...) e non
# "from debug_config import DEBUG_LOGGING": i listener leggono
# debug_config.DEBUG_LOGGING a runtime, quindi vedono sempre il
# valore corrente impostato qui, anche se l'hanno già importato.
debug_config.DEBUG_LOGGING = CONFIG.get("debug_logging", False)
 
# Applica eventuali override delle costanti del wake word
# listener dalla sezione "openwakeword" di config.json (soglie,
# tempistiche, direzione, AGC, beep, device). Chiavi assenti =
# restano i default hardcoded nel modulo. Deve avvenire PRIMA
# che il thread del listener venga avviato. Vedi la docstring di
# apply_config() in xvf3800_wakeword_listener.py per l'elenco
# completo delle chiavi supportate.
apply_config(CONFIG.get("openwakeword", {}))
 
# ==============================
# Coda audio e segnali di pronto
# ==============================
audio_queue = queue.Queue()
vosk_ready_event = threading.Event()  # segnala quando Vosk ha finito il caricamento
wakeword_ready_event = threading.Event()  # segnala quando il listener XVF3800 e' davvero in ascolto
 
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
        stop_event=stop_event,
        ready_event=wakeword_ready_event
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
 
# ==============================
# Attesa componenti davvero pronti
# ==============================
# vosk_ready_event scatta a modello+grammatica caricati;
# wakeword_ready_event scatta a device aperto e rumore
# calibrato. Solo a quel punto il servizio è realmente in
# grado di rispondere a un comando vocale.
vosk_ready_event.wait()
wakeword_ready_event.wait()
 
print("[MAIN] ================================================")
print("[MAIN] KeyVoice pronto e in ascolto")
print("[MAIN] ================================================")
 
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
 






