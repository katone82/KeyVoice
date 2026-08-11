import json
import os
import queue
import re
import threading
import unicodedata

import requests
from rapidfuzz import process


# ============================================================
# CODE CONDIVISE / STOP EVENT
# ============================================================

command_queue = queue.Queue()
ha_command_queue = queue.Queue()

stop_event = threading.Event()


# ============================================================
# VARIABILI GLOBALI
# ============================================================

DOMOTICA_FILE = ""
SYNONYMS_FILE = ""

HA_URL = ""
HA_TOKEN = ""

AZIONI = []
ENTITA = []
STANZE = []

MAPPING = {}
AZIONE_SYNONYMS = {}


# ============================================================
# NORMALIZZAZIONE TESTO
# ============================================================

def normalize(text: str) -> str:
    """
    Normalizza una stringa:
    - lowercase
    - rimozione accenti
    - rimozione punteggiatura
    - trim spazi
    """

    if not text:
        return ""

    text = text.lower()

    text = "".join(
        char
        for char in unicodedata.normalize("NFD", text)
        if unicodedata.category(char) != "Mn"
    )

    text = re.sub(
        r"[^\w\s]",
        "",
        text
    )

    return text.strip()


# ============================================================
# AGGIORNAMENTO DISPOSITIVI HOME ASSISTANT
# ============================================================

def aggiorna_dispositivi():
    global ENTITA
    global STANZE
    global MAPPING

    headers = {
        "Authorization": f"Bearer {HA_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(
            HA_URL,
            headers=headers,
            timeout=5
        )

        response.raise_for_status()

        data = response.json()

        print(
            "[FUZZY] Chiamata Home Assistant riuscita: "
            f"{len(data)} entità ricevute"
        )

        entita_local = {}
        stanze_local = set()

        for device in data:
            entity_id = device.get(
                "entity_id"
            )

            attributes = device.get(
                "attributes",
                {}
            )

            friendly_name = attributes.get(
                "friendly_name"
            )

            if not entity_id or not friendly_name:
                continue

            friendly_normalized = normalize(
                friendly_name
            )

            if not friendly_normalized:
                continue

            entita_local[
                friendly_normalized
            ] = entity_id

            # Mantengo la logica attuale:
            # parole con iniziale maiuscola considerate
            # possibili stanze.
            for part in friendly_name.split():

                if not part:
                    continue

                if part[0].isupper():
                    stanze_local.add(
                        normalize(part)
                    )

        MAPPING.clear()
        MAPPING.update(
            entita_local
        )

        ENTITA.clear()
        ENTITA.extend(
            entita_local.keys()
        )

        STANZE.clear()
        STANZE.extend(
            sorted(stanze_local)
        )

        domotica_data = {
            "azioni": AZIONI,
            "entita": ENTITA,
            "stanze": STANZE,
            "mapping": MAPPING
        }

        os.makedirs(
            os.path.dirname(DOMOTICA_FILE),
            exist_ok=True
        )

        with open(
            DOMOTICA_FILE,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                domotica_data,
                file,
                indent=2,
                ensure_ascii=False
            )

            file.flush()

            os.fsync(
                file.fileno()
            )

        print(
            "[FUZZY] domotica.json aggiornato con successo"
        )

        print(
            f"[FUZZY] File: {DOMOTICA_FILE}"
        )

        print(
            f"[FUZZY] Entità caricate: {len(ENTITA)}"
        )

        print(
            f"[FUZZY] Stanze rilevate: {len(STANZE)}"
        )

    except Exception as exc:

        print(
            "[FUZZY] Errore aggiornamento dispositivi: "
            f"{exc}"
        )

        raise SystemExit(
            "[FUZZY] Impossibile continuare "
            "senza domotica.json valido"
        )


# ============================================================
# CARICAMENTO DOMOTICA.JSON
# ============================================================

def carica_domotica():
    if (
        not os.path.exists(DOMOTICA_FILE)
        or os.path.getsize(DOMOTICA_FILE) == 0
    ):

        print(
            "[FUZZY] domotica.json non trovato o vuoto"
        )

        aggiorna_dispositivi()

        return

    try:
        with open(
            DOMOTICA_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            domotica_data = json.load(
                file
            )

        ENTITA.clear()

        ENTITA.extend(
            domotica_data.get(
                "entita",
                []
            )
        )

        STANZE.clear()

        STANZE.extend(
            domotica_data.get(
                "stanze",
                []
            )
        )

        MAPPING.clear()

        MAPPING.update(
            domotica_data.get(
                "mapping",
                {}
            )
        )

        print(
            "[FUZZY] domotica.json caricato correttamente"
        )

        print(
            f"[FUZZY] File: {DOMOTICA_FILE}"
        )

        print(
            f"[FUZZY] Entità trovate: {len(ENTITA)}"
        )

        print(
            f"[FUZZY] Stanze trovate: {len(STANZE)}"
        )

    except Exception as exc:

        print(
            "[FUZZY] Errore lettura domotica.json: "
            f"{exc}"
        )

        print(
            "[FUZZY] Ricreo domotica.json..."
        )

        aggiorna_dispositivi()


# ============================================================
# CARICAMENTO SINONIMI
# ============================================================

def carica_sinonimi():
    global AZIONE_SYNONYMS

    if not os.path.exists(
        SYNONYMS_FILE
    ):

        print(
            "[FUZZY] File sinonimi non trovato: "
            f"{SYNONYMS_FILE}"
        )

        AZIONE_SYNONYMS = {}

        return

    try:
        with open(
            SYNONYMS_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            AZIONE_SYNONYMS = (
                json.load(file)
            )

        print(
            "[FUZZY] Sinonimi caricati: "
            f"{SYNONYMS_FILE}"
        )

    except Exception as exc:

        print(
            "[FUZZY] Errore caricamento sinonimi: "
            f"{exc}"
        )

        AZIONE_SYNONYMS = {}


# ============================================================
# INIZIALIZZAZIONE FUZZY
# ============================================================

def init_fuzzy(config: dict):
    global DOMOTICA_FILE
    global SYNONYMS_FILE
    global HA_URL
    global HA_TOKEN
    global AZIONI

    config_path = config[
        "config_path"
    ]

    DOMOTICA_FILE = os.path.join(
        config_path,
        "domotica.json"
    )

    SYNONYMS_FILE = os.path.join(
        config_path,
        "azione_synonyms.json"
    )

    HA_URL = (
        f"{config['homeassistant']['url'].rstrip('/')}"
        "/api/states"
    )

    HA_TOKEN = config[
        "homeassistant"
    ][
        "token"
    ]

    AZIONI = config.get(
        "azioni",
        []
    )

    print(
        "[FUZZY] Inizializzazione..."
    )

    print(
        f"[FUZZY] Config path: {config_path}"
    )

    carica_sinonimi()

    carica_domotica()

    print(
        "[FUZZY] Inizializzazione completata"
    )


# ============================================================
# NORMALIZZAZIONE AZIONE
# ============================================================

def normalize_action(
    azione_rilevata: str
) -> str:

    azione_normalizzata = normalize(
        azione_rilevata
    )

    for canon, synonyms in (
        AZIONE_SYNONYMS.items()
    ):

        canon_normalized = normalize(
            canon
        )

        if (
            azione_normalizzata
            == canon_normalized
        ):
            return canon

        for synonym in synonyms:

            if (
                azione_normalizzata
                == normalize(synonym)
            ):
                return canon

    return azione_rilevata


# ============================================================
# RICERCA AZIONE
# ============================================================

def trova_azione(
    frase_norm: str
):

    if not AZIONI:
        return None

    match = process.extractOne(
        frase_norm,
        AZIONI
    )

    if not match:
        return None

    azione,
    score,
    _ = match

    if score <= 70:
        return None

    return normalize_action(
        azione
    )


# ============================================================
# RICERCA ENTITÀ
# ============================================================

def trova_entita(
    frase_norm: str
):

    if not ENTITA:
        return None

    frase_words = (
        frase_norm.split()
    )

    # --------------------------------------------------------
    # Prova prima sull'intera frase
    # --------------------------------------------------------

    full_match = process.extractOne(
        frase_norm,
        ENTITA
    )

    if (
        full_match
        and full_match[1] > 95
    ):
        return full_match[0]

    # --------------------------------------------------------
    # Fallback con N-GRAM
    # --------------------------------------------------------

    entita_finale = None
    best_score = 0

    for entita in ENTITA:

        entita_words = (
            entita.split()
        )

        entita_length = len(
            entita_words
        )

        if (
            len(frase_words)
            < entita_length
        ):
            continue

        max_score = 0

        for index in range(
            len(frase_words)
            - entita_length
            + 1
        ):

            ngram = " ".join(
                frase_words[
                    index:
                    index + entita_length
                ]
            )

            result = process.extractOne(
                ngram,
                [entita]
            )

            if not result:
                continue

            score = result[1]

            if score > max_score:
                max_score = score

        if max_score > best_score:

            best_score = max_score
            entita_finale = entita

        elif (
            max_score == best_score
            and entita_finale
            and entita_length
            > len(
                entita_finale.split()
            )
        ):

            entita_finale = entita

    if best_score < 95:
        return None

    return entita_finale


# ============================================================
# RICERCA STANZA
# ============================================================

def trova_stanza(
    frase_norm: str
):

    if not STANZE:
        return None

    stanza_finale = None
    best_score = 0

    for stanza in STANZE:

        result = process.extractOne(
            frase_norm,
            [stanza]
        )

        if not result:
            continue

        score = result[1]

        if score > best_score:

            best_score = score
            stanza_finale = stanza

    if best_score < 80:
        return None

    return stanza_finale


# ============================================================
# FUZZY PARSER
# ============================================================

def fuzzy_parse(
    frase: str
):

    frase_norm = normalize(
        frase
    )

    if not frase_norm:

        return (
            None,
            None,
            None,
            None
        )

    azione = trova_azione(
        frase_norm
    )

    entita = trova_entita(
        frase_norm
    )

    stanza = trova_stanza(
        frase_norm
    )

    entity_id = (
        MAPPING.get(entita)
        if entita
        else None
    )

    print(
        "[FUZZY] Parsing:"
    )

    print(
        f"[FUZZY]   frase={frase_norm}"
    )

    print(
        f"[FUZZY]   azione={azione}"
    )

    print(
        f"[FUZZY]   entita={entita}"
    )

    print(
        f"[FUZZY]   stanza={stanza}"
    )

    print(
        f"[FUZZY]   entity_id={entity_id}"
    )

    return (
        azione,
        entita,
        stanza,
        entity_id
    )


# ============================================================
# THREAD PROCESSAMENTO COMANDI
# ============================================================

def processa_comandi():
    import sys

    profile = (
        getattr(
            sys,
            "profile",
            None
        )
        or "default"
    )

    print(
        "[COMANDI] Thread avviato"
    )

    while not stop_event.is_set():

        try:

            frase = command_queue.get(
                timeout=1
            )

        except queue.Empty:
            continue

        try:

            (
                azione,
                entita,
                stanza,
                entity_id
            ) = fuzzy_parse(
                frase
            )

            if (
                azione
                and entita
                and entity_id
            ):

                result = {
                    "azione": azione,
                    "entita": entita,
                    "stanza": stanza,
                    "entity_id": entity_id
                }

                print(
                    "[COMANDI] Eseguo: "
                    f"{result}"
                )

                if profile != "test":

                    ha_command_queue.put(
                        result
                    )

                else:

                    print(
                        "[COMANDI] DEBUG: "
                        "comando NON inviato "
                        "a Home Assistant"
                    )

            else:

                print(
                    "[COMANDI] Comando non chiaro "
                    "o entità non trovata: "
                    f"{frase}"
                )

        except Exception as exc:

            print(
                "[COMANDI] Errore elaborazione comando: "
                f"{exc}"
            )

        finally:

            command_queue.task_done()


# ============================================================
# THREAD INVIO HOME ASSISTANT
# ============================================================

def ha_command_consumer():

    print(
        "[HA] Thread consumer avviato"
    )

    while not stop_event.is_set():

        try:

            command = (
                ha_command_queue.get(
                    timeout=1
                )
            )

        except queue.Empty:
            continue

        try:

            print(
                "[HA] Invio comando a Home Assistant: "
                f"{command}"
            )

            # TODO:
            # qui implementiamo la chiamata REST
            # vera verso Home Assistant.

        except Exception as exc:

            print(
                "[HA] Errore invio comando: "
                f"{exc}"
            )

        finally:

            ha_command_queue.task_done()