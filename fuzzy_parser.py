import json
import os
import queue
import re
import threading
import traceback
import unicodedata

import requests
from rapidfuzz import process

import sound_feedback
from numeri_italiani import PAROLA_A_NUMERO


# ============================================================
# CODE CONDIVISE
# ============================================================

command_queue = queue.Queue()
ha_command_queue = queue.Queue()
timer_command_queue = queue.Queue()

stop_event = threading.Event()


# ============================================================
# CONFIGURAZIONE RUNTIME
# ============================================================

DOMOTICA_FILE = ""
SYNONYMS_FILE = ""

HA_URL = ""
HA_TOKEN = ""

TIMER_SERVICE_URL = "http://127.0.0.1:8090"

AZIONI = []
ENTITA = []
STANZE = []

MAPPING = {}
AZIONE_SYNONYMS = {}


# ============================================================
# CONFIGURAZIONE TIMER VOCALE
# ============================================================

# Parole che indicano una richiesta di cancellazione del
# timer attivo. Devono comparire insieme alla parola "timer"
# nella frase riconosciuta.
TIMER_CANCELLA_KEYWORDS = {
    "cancella",
    "ferma",
    "annulla",
    "stop",
}

TIMER_UNITA_SECONDI = {
    "minuto": 60,
    "minuti": 60,
    "ora": 3600,
    "ore": 3600,
}

# Un solo timer vocale attivo alla volta: id dell'ultimo
# timer creato, usato dal comando di cancellazione.
_active_timer_id = None


# ============================================================
# NORMALIZZAZIONE TESTO
# ============================================================

def normalize(text: str) -> str:
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

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# ============================================================
# AGGIORNAMENTO DISPOSITIVI DA HOME ASSISTANT
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
            timeout=10
        )

        response.raise_for_status()

        data = response.json()

        print(
            f"[FUZZY] Home Assistant raggiunto: "
            f"{len(data)} entità ricevute"
        )

        entita_local = {}
        stanze_local = set()

        for item in data:
            entity_id = item.get("entity_id")

            attributes = item.get(
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

            for part in friendly_name.split():
                if not part:
                    continue

                if part[0].isupper():
                    stanza = normalize(part)

                    if stanza:
                        stanze_local.add(
                            stanza
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
            "[FUZZY] domotica.json aggiornato"
        )

        print(
            f"[FUZZY] File: {DOMOTICA_FILE}"
        )

        print(
            f"[FUZZY] Entità disponibili: "
            f"{len(ENTITA)}"
        )

        print(
            f"[FUZZY] Stanze rilevate: "
            f"{len(STANZE)}"
        )

    except Exception as exc:
        print(
            "[FUZZY] Errore aggiornamento "
            f"dispositivi Home Assistant: {exc}"
        )

        traceback.print_exc()

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
            "[FUZZY] domotica.json non presente "
            "o vuoto"
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
            "[FUZZY] domotica.json caricato"
        )

        print(
            f"[FUZZY] Entità: {len(ENTITA)}"
        )

        print(
            f"[FUZZY] Stanze: {len(STANZE)}"
        )

    except Exception as exc:
        print(
            "[FUZZY] Errore lettura "
            f"domotica.json: {exc}"
        )

        print(
            "[FUZZY] Ricreo domotica.json"
        )

        aggiorna_dispositivi()


# ============================================================
# CARICAMENTO SINONIMI AZIONI
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
            AZIONE_SYNONYMS = json.load(
                file
            )

        print(
            "[FUZZY] Sinonimi caricati: "
            f"{SYNONYMS_FILE}"
        )

        print(
            "[FUZZY] Azioni canoniche: "
            f"{list(AZIONE_SYNONYMS.keys())}"
        )

    except Exception as exc:
        print(
            "[FUZZY] Errore caricamento "
            f"sinonimi: {exc}"
        )

        AZIONE_SYNONYMS = {}


# ============================================================
# INIZIALIZZAZIONE
# ============================================================

def init_fuzzy(config: dict):
    global DOMOTICA_FILE
    global SYNONYMS_FILE
    global HA_URL
    global HA_TOKEN
    global TIMER_SERVICE_URL
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

    TIMER_SERVICE_URL = config.get(
        "timer_service",
        {}
    ).get(
        "url",
        TIMER_SERVICE_URL
    ).rstrip("/")

    AZIONI = config.get(
        "azioni",
        []
    )

    print(
        "[FUZZY] Inizializzazione"
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
# COSTRUZIONE VOCABOLARIO AZIONI
# ============================================================

def build_action_candidates():
    """
    Restituisce una mappa:

    variante_normalizzata -> azione_canonica

    Esempio:
    accendi     -> accendi
    accendere   -> accendi
    accendete   -> accendi
    spegnere    -> spegni
    """

    candidates = {}

    for canon, synonyms in (
        AZIONE_SYNONYMS.items()
    ):
        canon_normalized = normalize(
            canon
        )

        candidates[
            canon_normalized
        ] = canon

        for synonym in synonyms:
            synonym_normalized = normalize(
                synonym
            )

            if synonym_normalized:
                candidates[
                    synonym_normalized
                ] = canon

    return candidates


# ============================================================
# RICERCA AZIONE
# ============================================================

def trova_azione(
    frase_norm: str
):
    candidates = build_action_candidates()

    if not candidates:
        return None

    candidate_words = list(
        candidates.keys()
    )

    frase_words = frase_norm.split()

    best_score = 0
    best_action = None
    best_match = None

    # --------------------------------------------------------
    # PAROLE SINGOLE
    # --------------------------------------------------------

    for parola in frase_words:
        match = process.extractOne(
            parola,
            candidate_words
        )

        if not match:
            continue

        matched_text = match[0]
        score = match[1]

        if score > best_score:
            best_score = score
            best_match = matched_text
            best_action = candidates[
                matched_text
            ]

    # --------------------------------------------------------
    # BIGRAMMI
    # Utile per future azioni composte.
    # --------------------------------------------------------

    for index in range(
        len(frase_words) - 1
    ):
        ngram = " ".join(
            frase_words[
                index:index + 2
            ]
        )

        match = process.extractOne(
            ngram,
            candidate_words
        )

        if not match:
            continue

        matched_text = match[0]
        score = match[1]

        if score > best_score:
            best_score = score
            best_match = matched_text
            best_action = candidates[
                matched_text
            ]

    # --------------------------------------------------------
    # SOGLIA
    # --------------------------------------------------------

    if best_score < 70:
        print(
            "[FUZZY] Azione non riconosciuta "
            f"(score={best_score:.1f})"
        )

        return None

    print(
        "[FUZZY] Azione riconosciuta: "
        f"{best_action} "
        f"<- '{best_match}' "
        f"(score={best_score:.1f})"
    )

    return best_action


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
    # MATCH INTERA FRASE
    # --------------------------------------------------------

    full_match = process.extractOne(
        frase_norm,
        ENTITA
    )

    if (
        full_match
        and full_match[1] > 95
    ):
        print(
            "[FUZZY] Entità full-match: "
            f"{full_match[0]} "
            f"(score={full_match[1]:.1f})"
        )

        return full_match[0]

    # --------------------------------------------------------
    # N-GRAM
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
        print(
            "[FUZZY] Entità non riconosciuta "
            f"(score={best_score:.1f})"
        )

        return None

    print(
        "[FUZZY] Entità riconosciuta: "
        f"{entita_finale} "
        f"(score={best_score:.1f})"
    )

    return entita_finale


# ============================================================
# RICERCA STANZA
# ============================================================

def trova_stanza(
    frase_norm: str
):
    if not STANZE:
        return None

    best_score = 0
    stanza_finale = None

    frase_words = frase_norm.split()

    for stanza in STANZE:
        for parola in frase_words:
            result = process.extractOne(
                parola,
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
# RICONOSCIMENTO COMANDO TIMER
# ============================================================

def estrai_durata(
    frase_norm: str
):
    """
    Cerca nella frase la coppia "<numero> <unità>"
    (es. "dieci minuti", "un ora") e restituisce la durata
    in secondi, oppure None se non trovata.
    """

    parole = frase_norm.split()

    for indice, parola in enumerate(parole):

        if parola not in TIMER_UNITA_SECONDI:
            continue

        if indice == 0:
            continue

        precedente = parole[indice - 1]

        numero = PAROLA_A_NUMERO.get(
            precedente
        )

        if numero:
            return numero * TIMER_UNITA_SECONDI[parola]

    return None


def rileva_comando_timer(
    frase_norm: str
):
    """
    Riconosce un comando timer nella frase già normalizzata.

    Restituisce una tupla (tipo, durata_secondi):
      - ("avvia", durata) se viene richiesto un nuovo timer
      - ("cancella", None) se viene richiesta la cancellazione

    Restituisce None se la frase non è un comando timer.
    """

    parole = frase_norm.split()

    if "timer" not in parole:
        return None

    if TIMER_CANCELLA_KEYWORDS & set(parole):
        return ("cancella", None)

    durata = estrai_durata(
        frase_norm
    )

    if durata:
        return ("avvia", durata)

    return None


# ============================================================
# PARSER FUZZY PRINCIPALE
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

    print(
        f"[FUZZY] Analizzo: '{frase_norm}'"
    )

    azione_finale = trova_azione(
        frase_norm
    )

    entita_finale = trova_entita(
        frase_norm
    )

    stanza_finale = trova_stanza(
        frase_norm
    )

    entity_id = None

    if entita_finale:
        entity_id = MAPPING.get(
            entita_finale
        )

    print(
        "[FUZZY] Risultato:"
    )

    print(
        f"[FUZZY]   azione    = {azione_finale}"
    )

    print(
        f"[FUZZY]   entita    = {entita_finale}"
    )

    print(
        f"[FUZZY]   stanza    = {stanza_finale}"
    )

    print(
        f"[FUZZY]   entity_id = {entity_id}"
    )

    return (
        azione_finale,
        entita_finale,
        stanza_finale,
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
            frase_norm = normalize(
                frase
            )

            timer_cmd = rileva_comando_timer(
                frase_norm
            )

            if timer_cmd:
                tipo, durata = timer_cmd

                print(
                    "[COMANDI] Comando timer riconosciuto: "
                    f"{tipo} {durata}"
                )

                if profile != "test":
                    timer_command_queue.put(
                        {
                            "tipo": tipo,
                            "durata": durata
                        }
                    )

                else:
                    print(
                        "[COMANDI] Modalità TEST: "
                        "comando timer non inviato"
                    )

                continue

            (
                azione_finale,
                entita_finale,
                stanza_finale,
                entity_id
            ) = fuzzy_parse(
                frase
            )

            if (
                azione_finale
                and entita_finale
                and entity_id
            ):
                result = {
                    "azione": azione_finale,
                    "entita": entita_finale,
                    "stanza": stanza_finale,
                    "entity_id": entity_id
                }

                print(
                    "[COMANDI] Comando valido:"
                )

                print(
                    f"[COMANDI] {result}"
                )

                if profile != "test":
                    ha_command_queue.put(
                        result
                    )

                else:
                    print(
                        "[COMANDI] Modalità TEST: "
                        "comando non inviato"
                    )

            else:
                print(
                    "[COMANDI] Comando non chiaro "
                    "o entità non trovata:"
                )

                print(
                    f"[COMANDI] {frase}"
                )

                sound_feedback.play_command_error()

        except Exception as exc:
            print(
                "[COMANDI] Errore elaborazione "
                f"comando: {exc}"
            )

            traceback.print_exc()

        finally:
            command_queue.task_done()


# ============================================================
# THREAD HOME ASSISTANT
# ============================================================

def ha_command_consumer():
    print(
        "[HA] Thread consumer avviato"
    )

    while not stop_event.is_set():
        try:
            command = ha_command_queue.get(
                timeout=1
            )

        except queue.Empty:
            continue

        try:
            print(
                "[HA] Invio comando "
                "a Home Assistant:"
            )

            print(
                f"[HA] {command}"
            )

            #
            # TODO
            #
            # Qui inseriamo la chiamata REST
            # vera verso Home Assistant.
            #

        except Exception as exc:
            print(
                "[HA] Errore invio comando: "
                f"{exc}"
            )

            traceback.print_exc()

        finally:
            ha_command_queue.task_done()


# ============================================================
# THREAD TIMER SERVICE
# ============================================================

def timer_command_consumer():
    global _active_timer_id

    print(
        "[TIMER] Thread consumer avviato"
    )

    while not stop_event.is_set():
        try:
            comando = timer_command_queue.get(
                timeout=1
            )

        except queue.Empty:
            continue

        try:
            if comando["tipo"] == "avvia":

                risposta = requests.post(
                    f"{TIMER_SERVICE_URL}/timers",
                    json={
                        "duration": comando["durata"]
                    },
                    timeout=5,
                )

                if risposta.ok:
                    _active_timer_id = risposta.json()["id"]

                    print(
                        "[TIMER] Timer avviato: "
                        f"{_active_timer_id} "
                        f"({comando['durata']}s)"
                    )

                    sound_feedback.play_command_ok()

                else:
                    print(
                        "[TIMER] Errore avvio timer: "
                        f"{risposta.status_code} "
                        f"{risposta.text}"
                    )

                    sound_feedback.play_command_error()

            elif comando["tipo"] == "cancella":

                if not _active_timer_id:
                    print(
                        "[TIMER] Nessun timer attivo "
                        "da cancellare"
                    )

                    sound_feedback.play_command_error()

                else:
                    risposta = requests.post(
                        f"{TIMER_SERVICE_URL}/timers/"
                        f"{_active_timer_id}/cancel",
                        timeout=5,
                    )

                    if risposta.ok:
                        print(
                            "[TIMER] Timer cancellato: "
                            f"{_active_timer_id}"
                        )

                        sound_feedback.play_command_ok()

                        _active_timer_id = None

                    else:
                        print(
                            "[TIMER] Errore cancellazione "
                            "timer: "
                            f"{risposta.status_code} "
                            f"{risposta.text}"
                        )

                        sound_feedback.play_command_error()

        except Exception as exc:
            print(
                "[TIMER] Errore comunicazione con "
                f"timer_service: {exc}"
            )

            traceback.print_exc()

            sound_feedback.play_command_error()

        finally:
            timer_command_queue.task_done()