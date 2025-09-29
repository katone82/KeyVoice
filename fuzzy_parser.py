import os
import json
import queue
import threading
ha_command_queue = queue.Queue()
import unicodedata
import re
from rapidfuzz import process
import requests

# =======================
# Coda condivisa e stop event
# =======================
command_queue = queue.Queue()
stop_event = threading.Event()

# =======================
# Variabili globali
# =======================
DOMOTICA_FILE = ""
HA_URL = ""
HA_TOKEN = ""
AZIONI = []
ENTITA = []
STANZE = []
MAPPING = {}
AZIONE_SYNONYMS = {}

# =======================
# Aggiorna dispositivi da Home Assistant
# =======================
def aggiorna_dispositivi():
    global AZIONI, MAPPING, ENTITA, STANZE

    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    try:
        response = requests.get(HA_URL, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        print(f"[FUZZY] ✅ Chiamata Home Assistant riuscita, {len(data)} entità ricevute")

        entita_local = {}
        stanze_local = set()
        for d in data:
            friendly = d.get("attributes", {}).get("friendly_name")
            entity = d.get("entity_id")
            if friendly and entity:
                friendly_clean = friendly.strip().lower()
                entita_local[friendly_clean] = entity
                for part in friendly.split():
                    if part[0].isupper():
                        stanze_local.add(part.lower())

        MAPPING = entita_local
        ENTITA = list(entita_local.keys())
        STANZE = list(stanze_local)

        domotica_data = {
            "azioni": AZIONI,
            "entita": ENTITA,
            "stanze": STANZE,
            "mapping": MAPPING
        }

        os.makedirs(os.path.dirname(DOMOTICA_FILE), exist_ok=True)
        with open(DOMOTICA_FILE, "w", encoding="utf-8") as f:
            json.dump(domotica_data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())

        print(f"[FUZZY] ✅ domotica.json aggiornato / creato con successo")
        print(f"[FUZZY] Mapping friendly_name -> entity_id: {MAPPING}")

    except Exception as e:
        print(f"[FUZZY] ❌ Errore aggiornamento dispositivi: {e}")
        raise SystemExit("[FUZZY] Impossibile continuare senza domotica.json valido")

# =======================
# Inizializzazione fuzzy parser
# =======================
def init_fuzzy(config, sinonimi_file="azione_synonyms.json"):
    global DOMOTICA_FILE, HA_URL, HA_TOKEN, AZIONI, AZIONE_SYNONYMS

    DOMOTICA_FILE = config["vosk"]["domotica_json"]
    HA_URL = f"{config['homeassistant']['url'].rstrip('/')}/api/states"
    HA_TOKEN = config["homeassistant"]["token"]
    AZIONI = config["azioni"]

    # Carica sinonimi azioni
    if os.path.exists(sinonimi_file):
        with open(sinonimi_file, "r", encoding="utf-8") as f:
            AZIONE_SYNONYMS = json.load(f)
    else:
        AZIONE_SYNONYMS = {}

    # Controlla se domotica.json esiste e non è vuoto
    if not os.path.exists(DOMOTICA_FILE) or os.stat(DOMOTICA_FILE).st_size == 0:
        print(f"[FUZZY] domotica.json non trovato o vuoto, creo il file...")
        aggiorna_dispositivi()
    else:
        print(f"[FUZZY] domotica.json già presente, caricamento dati...")
        try:
            with open(DOMOTICA_FILE, "r", encoding="utf-8") as f:
                domotica_data = json.load(f)
                ENTITA[:] = domotica_data.get("entita", [])
                STANZE[:] = domotica_data.get("stanze", [])
                MAPPING.clear()
                MAPPING.update(domotica_data.get("mapping", {}))
                print(f"[FUZZY] domotica.json caricato correttamente, {len(ENTITA)} entità trovate")
                print(f"[FUZZY] Mapping friendly_name -> entity_id: {MAPPING}")
        except Exception as e:
            print(f"[FUZZY] ERRORE lettura domotica.json: {e}, ricreo il file...")
            aggiorna_dispositivi()

# =======================
# Normalizzazione azione
# =======================
def normalize_action(azione_rilevata: str) -> str:
    for canon, syn_list in AZIONE_SYNONYMS.items():
        if azione_rilevata in syn_list or azione_rilevata == canon:
            return canon
    return azione_rilevata

# =======================
# Parsing fuzzy multi-word intelligente
# =======================
def fuzzy_parse(frase: str):
    # Normalize: lowercase, remove accents, remove punctuation
    def normalize(text):
        text = text.lower()
        text = ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn')
        text = re.sub(r'[^\w\s]', '', text)
        return text.strip()

    frase_norm = normalize(frase)
    frase_words = frase_norm.split()

    # Azione
    azione_match = process.extractOne(frase_norm, AZIONI)
    azione = normalize_action(azione_match[0]) if azione_match and azione_match[1] > 70 else None

    # Entità: try full phrase first, then n-gram
    entita_finale = None
    best_score = 0
    # Try full phrase match
    ent_full = process.extractOne(frase_norm, ENTITA)
    if ent_full and ent_full[1] > 95:
        entita_finale = ent_full[0]
        best_score = ent_full[1]
    else:
        # Fallback: n-gram search
        for ent in ENTITA:
            ent_words = ent.split()
            max_score = 0
            for i in range(len(frase_words) - len(ent_words) + 1):
                ngram = " ".join(frase_words[i:i+len(ent_words)])
                score = process.extractOne(ngram, [ent])[1]
                if score > max_score:
                    max_score = score
            if max_score > best_score or (max_score == best_score and entita_finale and len(ent_words) > len(entita_finale.split())):
                best_score = max_score
                entita_finale = ent
        if best_score < 95:
            entita_finale = None

    entity_id = MAPPING.get(entita_finale) if entita_finale else None

    # Stanza (facoltativa, stricter)
    best_score_stanza = 0
    stanza_finale = None
    for st in STANZE:
        score = process.extractOne(frase_norm, [st])[1]
        if score > best_score_stanza:
            best_score_stanza = score
            stanza_finale = st
    if best_score_stanza < 80:
        stanza_finale = None

    return azione, entita_finale, stanza_finale, entity_id

# =======================
# Thread per processare comandi
# =======================
def processa_comandi():
    import sys
    PROFILE = getattr(sys, 'profile', None) or 'default'
    while not stop_event.is_set():
        try:
            frase = command_queue.get(timeout=1)
        except queue.Empty:
            continue

        azione, entita, stanza, entity_id = fuzzy_parse(frase)
        if azione and entita and entity_id:
            result = {
                "azione": azione,
                "entita": entita,
                "stanza": stanza if stanza else None,
                "entity_id": entity_id
            }
            print(f"[COMANDI] Eseguo: {result}")
            if PROFILE != 'test':
                ha_command_queue.put(result)
            else:
                print("[COMANDI] DEBUG: comando NON inviato a Home Assistant (profilo test)")
        else:
            print(f"[COMANDI] Comando non chiaro o entità non trovata: {frase}")

# =======================
# Thread per inviare comandi a Home Assistant
# =======================
def ha_command_consumer():
    while not stop_event.is_set():
        try:
            cmd = ha_command_queue.get(timeout=1)
        except queue.Empty:
            continue
        # Qui va la logica per inviare il comando a Home Assistant
        print(f"[HA] Invio comando a Home Assistant: {cmd}")
        # TODO: implementa la chiamata HTTP o altro metodo
