import json
import threading
import queue
import time
import os
import sys

import vosk

import debug_config
import sound_feedback
from vosk_listener import vosk_listener
from xvf3800_wakeword_listener import WakeWordListener, apply_config
from fuzzy_parser import init_fuzzy, processa_comandi, command_queue, stop_event, timer_command_consumer, gestisci_timer_finito
from timer_mqtt import avvia_listener_timer_mqtt
from ha_command_consumer import ha_command_consumer

# ==============================
# CARICA CONFIGURAZIONE ESTERNA
# ==============================
with open("config/config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

# Segreti (token, password) tenuti fuori da config.json/git — vedi
# config/secrets.json (non versionato, richiesto a parte).
with open("config/secrets.json", "r", encoding="utf-8") as f:
    SECRETS = json.load(f)

CONFIG['homeassistant']['token'] = SECRETS['homeassistant']['token']

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

# Suoni di conferma comando (fatto/non fatto), sezione opzionale
# "command_feedback" di config.json. Vedi la docstring di
# sound_feedback.apply_config() per le chiavi supportate.
sound_feedback.apply_config(CONFIG.get("command_feedback", {}))

# ==============================
# Modello Vosk condiviso
# ==============================
# Caricato UNA volta qui e passato sia al thread comandi
# (vosk_listener) sia al thread wake word (WakeWordListener, che
# lo usa per il recognizer a grammatica chiusa "hey jarvis"):
# vosk.Model puo' essere usato da piu' KaldiRecognizer
# contemporaneamente, quindi non serve caricarlo due volte e
# tenere doppia RAM occupata per lo stesso modello.
print("[MAIN] Caricamento modello Vosk condiviso...")
VOSK_MODEL = vosk.Model(CONFIG["vosk"]["model_path"])
print("[MAIN] Modello Vosk caricato")

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
        ready_event=vosk_ready_event,
        model=VOSK_MODEL
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
        ready_event=wakeword_ready_event,
        vosk_model=VOSK_MODEL
    )
    listener.run()

# ==============================
# Avvio thread
# ==============================
t_vosk = threading.Thread(target=vosk_thread, daemon=True)
t_wakeword = threading.Thread(target=wakeword_thread, daemon=True)
t_fuzzy = threading.Thread(target=processa_comandi, daemon=True)
ha_url = CONFIG['homeassistant']['url'].rstrip('/') + '/api/services'
ha_token = CONFIG['homeassistant']['token']
t_ha = threading.Thread(target=ha_command_consumer, args=(ha_url, ha_token), daemon=True)
t_timer = threading.Thread(target=timer_command_consumer, daemon=True)

t_vosk.start()
t_wakeword.start()
t_fuzzy.start()
t_ha.start()
t_timer.start()

# Ascolto MQTT della notifica di fine timer pubblicata
# dall'automazione HA (vedi ha/keyvoice_timer_automation.yaml).
# Non è un thread separato: il client paho-mqtt gestisce da
# solo il proprio loop in background (client.loop_start()).
timer_mqtt_client = avvia_listener_timer_mqtt(
    CONFIG,
    on_finished=gestisci_timer_finito
)

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
    t_timer.join()
    print("[MAIN] Tutto terminato.")
