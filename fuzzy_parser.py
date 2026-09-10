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
HA_SERVICES_URL = ""

AZIONI = []
ENTITA = []
STANZE = []

MAPPING = {}
AZIONE_SYNONYMS = {}


# ============================================================
# CONFIGURAZIONE TIMER VOCALE
# ============================================================
#
# I timer sono gestiti da Home Assistant (dominio "timer"), non
# da KeyVoice: qui c'è solo un pool di entità HA predefinite
# (timer.keyvoice_1 .. timer.keyvoice_N, vedi ha/) tra cui
# scegliere uno slot libero all'avvio. Il nome pronunciato
# (es. "torta") NON esiste in HA: viene tenuto solo qui, in
# memoria, associato allo slot occupato, solo per sapere cosa
# annunciare/loggare alla notifica di fine timer via MQTT.

TIMER_ENTITY_PREFIX = "timer.keyvoice_"
TIMER_POOL_SIZE = 5

TIMER_ENTITIES = [
    f"{TIMER_ENTITY_PREFIX}{indice}"
    for indice in range(1, TIMER_POOL_SIZE + 1)
]

# Parole che indicano una richiesta di cancellazione di un
# timer. Devono comparire insieme alla parola "timer" nella
# frase riconosciuta.
TIMER_CANCELLA_KEYWORDS = {
    "cancella",
    "ferma",
    "annulla",
    "stop",
}

# Parola chiave opzionale che introduce esplicitamente il nome:
# "crea un timer chiamato torta di dieci minuti".
TIMER_NOME_KEYWORD = "chiamato"

# Parole di raccordo da scartare se catturate per errore come
# parte del nome (es. articoli prima di "timer").
TIMER_PAROLE_DA_IGNORARE = {
    "un", "uno", "una", "il", "lo", "la", "i", "gli", "le",
}

TIMER_UNITA_SECONDI = {
    "minuto": 60,
    "minuti": 60,
    "ora": 3600,
    "ore": 3600,
}

# Slot occupati: entity_id -> nome pronunciato (può essere
# None se il timer è stato avviato senza nome).
_timer_slots = {}
_timer_slots_lock = threading.Lock()


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
    global HA_SERVICES_URL
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

    HA_SERVICES_URL = (
        f"{config['homeassistant']['url'].rstrip('/')}"
        "/api/services"
    )

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

TIMER_CONNETTORI_FINALI = {
    "di", "per", "da",
}


def _trova_durata_con_indice(
    parole
):
    """
    Cerca la coppia "<numero> <unità>" (es. "dieci minuti").

    Restituisce (durata_secondi, indice_numero), oppure
    (None, None) se non trovata. indice_numero è la posizione
    della parola-numero nella lista, usata da
    estrai_nome_timer() per capire dove finisce il nome e
    inizia la durata.
    """

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
            return (
                numero * TIMER_UNITA_SECONDI[parola],
                indice - 1
            )

    return None, None


def estrai_durata(
    frase_norm: str
):
    durata, _ = _trova_durata_con_indice(
        frase_norm.split()
    )

    return durata


def estrai_nome_timer(
    frase_norm: str,
    indice_numero=None
):
    """
    Estrae il nome libero del timer: tutto ciò che sta tra
    "timer" (o "timer chiamato") e l'inizio della durata
    (es. "crea un timer torta di dieci minuti" -> "torta").

    Nessun elenco chiuso di nomi: qualunque parola pronunciata
    in quella posizione viene presa così com'è (per questo la
    frase deve arrivare dalla passata di dettatura libera, non
    dalla grammatica chiusa — vedi vosk_listener.py).

    Restituisce None se non c'è nulla in quella posizione.
    """

    parole = frase_norm.split()

    if "timer" not in parole:
        return None

    indice_timer = parole.index(
        "timer"
    )

    if indice_numero is None:
        _, indice_numero = _trova_durata_con_indice(
            parole
        )

    fine = (
        indice_numero
        if indice_numero is not None
        else len(parole)
    )

    segmento = parole[
        indice_timer + 1: fine
    ]

    if (
        segmento
        and segmento[0] == TIMER_NOME_KEYWORD
    ):
        segmento = segmento[1:]

    segmento = [
        parola
        for parola in segmento
        if parola not in TIMER_PAROLE_DA_IGNORARE
        and parola not in TIMER_CONNETTORI_FINALI
    ]

    if not segmento:
        return None

    return " ".join(segmento)


def rileva_comando_timer(
    frase_norm: str
):
    """
    Riconosce un comando timer nella frase già normalizzata.

    Restituisce una tupla (tipo, durata_secondi, nome):
      - ("avvia", durata, nome) per un nuovo timer
      - ("cancella", None, nome) per una cancellazione
        (nome è None se non specificato)

    Restituisce None se la frase non è un comando timer.
    """

    parole = frase_norm.split()

    if "timer" not in parole:
        return None

    if TIMER_CANCELLA_KEYWORDS & set(parole):

        nome = estrai_nome_timer(
            frase_norm,
            indice_numero=len(parole)
        )

        return ("cancella", None, nome)

    durata, indice_numero = _trova_durata_con_indice(
        parole
    )

    if durata:

        nome = estrai_nome_timer(
            frase_norm,
            indice_numero=indice_numero
        )

        return ("avvia", durata, nome)

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
                tipo, durata, nome = timer_cmd

                print(
                    "[COMANDI] Comando timer riconosciuto: "
                    f"{tipo} durata={durata} nome={nome}"
                )

                if profile != "test":
                    timer_command_queue.put(
                        {
                            "tipo": tipo,
                            "durata": durata,
                            "nome": nome
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
# TIMER: POOL SLOT HOME ASSISTANT
# ============================================================

def _formatta_durata(
    secondi: int
) -> str:
    ore, resto = divmod(
        int(secondi),
        3600
    )

    minuti, sec = divmod(
        resto,
        60
    )

    return (
        f"{ore:02d}:{minuti:02d}:{sec:02d}"
    )


def _trova_slot_libero():
    with _timer_slots_lock:
        for entity_id in TIMER_ENTITIES:
            if entity_id not in _timer_slots:
                return entity_id

    return None


def _trova_slot_per_nome(
    nome: str
):
    with _timer_slots_lock:
        for entity_id, nome_slot in _timer_slots.items():
            if nome_slot and nome_slot == nome:
                return entity_id

    return None


def _slot_occupati():
    with _timer_slots_lock:
        return dict(_timer_slots)


def gestisci_timer_finito(
    entity_id: str
):
    """
    Chiamata dal listener MQTT (timer_mqtt.py) quando arriva
    la notifica di fine timer pubblicata dall'automazione HA
    su keyvoice/timer/finished.
    """

    with _timer_slots_lock:
        nome = _timer_slots.pop(
            entity_id,
            None
        )

    print(
        "[TIMER] Timer terminato: "
        f"{entity_id} "
        f"(nome={nome})"
    )

    sound_feedback.play_command_ok()


# ============================================================
# TIMER: CHIAMATE HOME ASSISTANT
# ============================================================

def _ha_timer_start(
    entity_id: str,
    durata_secondi: int
) -> bool:
    risposta = requests.post(
        f"{HA_SERVICES_URL}/timer/start",
        headers={
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "entity_id": entity_id,
            "duration": _formatta_durata(
                durata_secondi
            ),
        },
        timeout=5,
    )

    if not risposta.ok:
        print(
            "[TIMER] Errore avvio timer HA: "
            f"{risposta.status_code} "
            f"{risposta.text}"
        )

    return risposta.ok


def _ha_timer_cancel(
    entity_id: str
) -> bool:
    risposta = requests.post(
        f"{HA_SERVICES_URL}/timer/cancel",
        headers={
            "Authorization": f"Bearer {HA_TOKEN}",
            "Content-Type": "application/json",
        },
        json={
            "entity_id": entity_id,
        },
        timeout=5,
    )

    if not risposta.ok:
        print(
            "[TIMER] Errore cancellazione timer HA: "
            f"{risposta.status_code} "
            f"{risposta.text}"
        )

    return risposta.ok


# ============================================================
# THREAD TIMER
# ============================================================

def timer_command_consumer():
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

                entity_id = _trova_slot_libero()

                if not entity_id:
                    print(
                        "[TIMER] Pool esaurito: "
                        f"{TIMER_POOL_SIZE} timer già attivi"
                    )

                    sound_feedback.play_command_error()

                else:
                    ok = _ha_timer_start(
                        entity_id,
                        comando["durata"]
                    )

                    if ok:
                        with _timer_slots_lock:
                            _timer_slots[entity_id] = (
                                comando["nome"]
                            )

                        print(
                            "[TIMER] Timer avviato: "
                            f"{entity_id} "
                            f"nome={comando['nome']} "
                            f"({comando['durata']}s)"
                        )

                        sound_feedback.play_command_ok()

                    else:
                        sound_feedback.play_command_error()

            elif comando["tipo"] == "cancella":

                nome = comando["nome"]

                if nome:
                    entity_id = _trova_slot_per_nome(
                        nome
                    )

                    if not entity_id:
                        print(
                            "[TIMER] Nessun timer attivo "
                            f"chiamato '{nome}'"
                        )

                        sound_feedback.play_command_error()
                        entity_id = None

                else:
                    occupati = _slot_occupati()

                    if len(occupati) == 1:
                        entity_id = next(
                            iter(occupati)
                        )

                    elif len(occupati) == 0:
                        print(
                            "[TIMER] Nessun timer attivo "
                            "da cancellare"
                        )

                        sound_feedback.play_command_error()
                        entity_id = None

                    else:
                        print(
                            "[TIMER] Più timer attivi "
                            f"({list(occupati.values())}), "
                            "specifica il nome per cancellare"
                        )

                        sound_feedback.play_command_error()
                        entity_id = None

                if entity_id:
                    ok = _ha_timer_cancel(
                        entity_id
                    )

                    if ok:
                        with _timer_slots_lock:
                            _timer_slots.pop(
                                entity_id,
                                None
                            )

                        print(
                            "[TIMER] Timer cancellato: "
                            f"{entity_id}"
                        )

                        sound_feedback.play_command_ok()

                    else:
                        sound_feedback.play_command_error()

        except Exception as exc:
            print(
                "[TIMER] Errore comunicazione con "
                f"Home Assistant: {exc}"
            )

            traceback.print_exc()

            sound_feedback.play_command_error()

        finally:
            timer_command_queue.task_done()