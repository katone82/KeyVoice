import json
import os
import threading
import time
import unicodedata
import re
from queue import Empty, Queue

import numpy as np
import vosk
from scipy.signal import resample_poly

from numeri_italiani import (
    numero_in_parole,
    parola_numero_singolare,
)


# ============================================================
# CONFIGURAZIONE
# ============================================================

VOSK_SAMPLE_RATE = 16_000

DOMOTICA_FILENAME = "domotica.json"
SYNONYMS_FILENAME = "azione_synonyms.json"

UNKNOWN_TOKEN = "[unk]"


# ============================================================
# CONFIGURAZIONE TIMER
# ============================================================

# Prefissi accettati per avviare un timer. La durata (minuti
# o ore) viene aggiunta subito dopo, es.:
#
#   "imposta timer di dieci minuti"
#   "avvia un timer di due ore"
#
TIMER_AVVIO_PREFISSI = [
    "timer",
    "imposta timer",
    "imposta un timer",
    "avvia timer",
    "avvia un timer",
    "metti un timer",
    "crea timer",
]

TIMER_CANCELLA_FRASI = [
    "cancella timer",
    "cancella il timer",
    "ferma timer",
    "ferma il timer",
    "annulla timer",
    "stop timer",
    "spegni timer",
    "spegni il timer",
]

TIMER_MAX_MINUTI = 60
TIMER_MAX_ORE = 12


# ============================================================
# NORMALIZZAZIONE TESTO
# ============================================================

def normalize_text(text: str) -> str:
    """
    Normalizza il testo utilizzato nella grammatica Vosk:
    - lowercase
    - rimozione accenti
    - rimozione punteggiatura
    - normalizzazione spazi
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
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# ============================================================
# LETTURA JSON
# ============================================================

def load_json_file(
    filename: str
) -> dict:
    try:
        with open(
            filename,
            "r",
            encoding="utf-8"
        ) as file:
            return json.load(file)

    except FileNotFoundError:
        print(
            f"[VOLK] File non trovato: {filename}"
        )

    except json.JSONDecodeError as exc:
        print(
            f"[VOLK] JSON non valido "
            f"{filename}: {exc}"
        )

    except Exception as exc:
        print(
            f"[VOLK] Errore lettura "
            f"{filename}: {exc}"
        )

    return {}


# ============================================================
# PREPARAZIONE AUDIO
# ============================================================

def prepare_audio(
    audio_buffer,
    sample_rate: int
) -> np.ndarray:
    """
    Prepara l'audio per Vosk:
    - mono
    - int16
    - 16 kHz
    - normalizzazione del livello
    """

    audio_array = np.asarray(
        audio_buffer,
        dtype=np.int16
    )

    if audio_array.size == 0:
        return audio_array

    # ========================================================
    # RESAMPLE
    # ========================================================

    if sample_rate != VOSK_SAMPLE_RATE:

        audio_array = resample_poly(
            audio_array.astype(np.float32),
            VOSK_SAMPLE_RATE,
            sample_rate
        )

        audio_array = np.clip(
            audio_array,
            -32768,
            32767
        ).astype(np.int16)

    # ========================================================
    # NORMALIZZAZIONE VOLUME
    # ========================================================

    max_value = np.max(
        np.abs(
            audio_array.astype(np.int32)
        )
    )

    if max_value > 0:

        gain = 32767.0 / max_value

        # Evitiamo gain esagerati sul solo rumore.
        max_gain = 3.0

        gain = min(
            gain,
            max_gain
        )

        audio_array = np.clip(
            audio_array.astype(np.float32) * gain,
            -32768,
            32767
        ).astype(np.int16)

    return audio_array


# ============================================================
# PATH CONFIG
# ============================================================

def get_config_path() -> str:
    """
    KeyVoice/config

    Non richiede modifiche a run_service.py.
    """

    project_path = os.path.dirname(
        os.path.abspath(__file__)
    )

    return os.path.join(
        project_path,
        "config"
    )


# ============================================================
# CARICAMENTO ENTITÀ
# ============================================================

def load_entities(
    config_path: str
) -> list[str]:

    domotica_file = os.path.join(
        config_path,
        DOMOTICA_FILENAME
    )

    data = load_json_file(
        domotica_file
    )

    entities = data.get(
        "entita",
        []
    )

    result = set()

    for entity in entities:

        normalized = normalize_text(
            entity
        )

        if normalized:
            result.add(
                normalized
            )

    return sorted(
        result
    )


# ============================================================
# CARICAMENTO AZIONI
# ============================================================

def load_actions(
    config_path: str
) -> dict[str, list[str]]:

    synonyms_file = os.path.join(
        config_path,
        SYNONYMS_FILENAME
    )

    data = load_json_file(
        synonyms_file
    )

    result = {}

    for canonical, synonyms in data.items():

        canonical_normalized = normalize_text(
            canonical
        )

        if not canonical_normalized:
            continue

        variants = {
            canonical_normalized
        }

        for synonym in synonyms:

            normalized = normalize_text(
                synonym
            )

            if normalized:
                variants.add(
                    normalized
                )

        result[
            canonical_normalized
        ] = sorted(
            variants
        )

    return result


# ============================================================
# COSTRUZIONE GRAMMATICA TIMER
# ============================================================

def build_timer_grammar() -> set[str]:
    """
    Genera le frasi del timer vocale.

    La grammatica Vosk è chiusa: non esiste dettatura libera
    di numeri, quindi ogni combinazione prefisso/durata deve
    essere elencata esplicitamente.
    """

    frasi = set()

    for prefisso in TIMER_AVVIO_PREFISSI:

        for minuti in range(1, TIMER_MAX_MINUTI + 1):

            unita = (
                "minuto"
                if minuti == 1
                else "minuti"
            )

            frasi.add(
                normalize_text(
                    f"{prefisso} di "
                    f"{parola_numero_singolare(minuti)} "
                    f"{unita}"
                )
            )

        for ore in range(1, TIMER_MAX_ORE + 1):

            unita = (
                "ora"
                if ore == 1
                else "ore"
            )

            frasi.add(
                normalize_text(
                    f"{prefisso} di "
                    f"{parola_numero_singolare(ore)} "
                    f"{unita}"
                )
            )

    for frase in TIMER_CANCELLA_FRASI:
        frasi.add(
            normalize_text(
                frase
            )
        )

    return frasi


# ============================================================
# COSTRUZIONE GRAMMATICA
# ============================================================

def build_vosk_grammar() -> list[str]:
    """
    Genera la grammatica specifica per KeyVoice.

    Esempi:

        accendi luce cucina
        accendere luce cucina
        spegni luce cucina
        spegnere luce cucina

    Vengono aggiunte anche alcune forme naturali:

        accendi la luce cucina
        spegni la luce cucina
    """

    config_path = get_config_path()

    print(
        f"[VOLK] Config path: {config_path}"
    )

    entities = load_entities(
        config_path
    )

    actions = load_actions(
        config_path
    )

    grammar = set()

    # ========================================================
    # PAROLE / ENTITÀ BASE
    # ========================================================

    for entity in entities:
        grammar.add(
            entity
        )

    action_variants = set()

    for variants in actions.values():

        for action in variants:

            action_variants.add(
                action
            )

            grammar.add(
                action
            )

    # ========================================================
    # COMANDI COMPLETI
    # ========================================================

    articles = [
        "",
        "la ",
        "il ",
        "lo ",
        "le ",
        "l "
    ]

    for action in action_variants:

        for entity in entities:

            for article in articles:

                command = (
                    f"{action} "
                    f"{article}"
                    f"{entity}"
                )

                grammar.add(
                    normalize_text(
                        command
                    )
                )

    # ========================================================
    # TIMER
    # ========================================================

    timer_grammar = build_timer_grammar()

    grammar.update(
        timer_grammar
    )

    # ========================================================
    # UNKNOWN
    # ========================================================

    grammar.add(
        UNKNOWN_TOKEN
    )

    result = sorted(
        grammar
    )

    print(
        "[VOLK] Grammatica KeyVoice:"
    )

    print(
        f"[VOLK]   entità: "
        f"{len(entities)}"
    )

    print(
        f"[VOLK]   varianti azioni: "
        f"{len(action_variants)}"
    )

    print(
        f"[VOLK]   frasi timer: "
        f"{len(timer_grammar)}"
    )

    print(
        f"[VOLK]   frasi totali: "
        f"{len(result)}"
    )

    return result


# ============================================================
# CREAZIONE RECOGNIZER
# ============================================================

def create_recognizer(
    model,
    grammar: list[str]
):
    """
    Crea il recognizer Vosk con grammatica KeyVoice.
    """

    grammar_json = json.dumps(
        grammar,
        ensure_ascii=False
    )

    recognizer = vosk.KaldiRecognizer(
        model,
        VOSK_SAMPLE_RATE,
        grammar_json
    )

    # Non ci servono risultati parziali al fuzzy parser.
    # Il listener riceve già un comando completo delimitato
    # dal VAD.

    return recognizer


def create_free_recognizer(
    model
):
    """
    Recognizer SENZA grammatica (dettatura libera).

    La grammatica chiusa non può in nessun caso restituire
    una parola che non è già stata elencata: qualsiasi parola
    fuori vocabolario diventa [unk], senza modo di recuperarne
    il testo. Per il nome libero del timer ("crea un timer
    torta di dieci minuti") serve quindi una seconda passata
    di dettatura libera sullo stesso audio, usata SOLO quando
    la prima passata (grammatica) fallisce o contiene la
    parola "timer" — per le altre frasi (domotica) resta tutto
    invariato, sulla passata a grammatica chiusa più accurata.
    """

    return vosk.KaldiRecognizer(
        model,
        VOSK_SAMPLE_RATE
    )


# ============================================================
# PARSING RISULTATO
# ============================================================

def get_final_text(
    recognizer
) -> str:
    """
    Recupera esclusivamente il risultato finale.
    """

    result_json = (
        recognizer.FinalResult()
    )

    result = json.loads(
        result_json
    )

    return result.get(
        "text",
        ""
    ).strip()


# ============================================================
# VOSK LISTENER
# ============================================================

def vosk_listener(
    audio_queue: Queue,
    stop_event: threading.Event,
    config: dict,
    command_queue: Queue = None,
    ready_event=None,
    model=None
) -> None:
    """
    Thread Vosk di KeyVoice.

    Ogni elemento ricevuto da audio_queue rappresenta
    un comando completo già delimitato dal VAD.

    Flusso:

        audio_queue
            ↓
        prepare_audio
            ↓
        Vosk + grammatica KeyVoice
            ↓
        FinalResult
            ↓
        command_queue
            ↓
        fuzzy parser

    Nessun PartialResult viene mai inviato al fuzzy.
    """

    print(
        "[VOLK] Thread partito"
    )

    # ========================================================
    # MODEL
    # ========================================================
    #
    # Se il chiamante ha già passato un'istanza (vedi run_service.
    # py: lo stesso vosk.Model viene condiviso con il recognizer
    # della wake word in xvf3800_wakeword_listener.py, per non
    # tenere due copie del modello in RAM), la riusiamo invece di
    # ricaricarla da disco.

    if model is None:

        model_path = config[
            "model_path"
        ]

        print(
            "[VOLK] "
            f"Caricamento modello da: "
            f"{model_path}"
        )

        try:

            model = vosk.Model(
                model_path
            )

        except Exception as exc:

            print(
                "[VOLK] "
                f"ERRORE caricamento modello: "
                f"{exc}"
            )

            return

        print(
            "[VOLK] Modello caricato"
        )

    else:

        print(
            "[VOLK] Modello Vosk condiviso "
            "(già caricato altrove)"
        )

    # ========================================================
    # GRAMMAR
    # ========================================================

    try:

        grammar = (
            build_vosk_grammar()
        )

    except Exception as exc:

        print(
            "[VOLK] "
            "ERRORE costruzione grammatica: "
            f"{exc}"
        )

        return

    if len(grammar) <= 1:

        print(
            "[VOLK] "
            "ERRORE grammatica vuota"
        )

        return

    # ========================================================
    # RECOGNIZER
    # ========================================================

    try:

        recognizer = create_recognizer(
            model,
            grammar
        )

    except Exception as exc:

        print(
            "[VOLK] "
            "ERRORE creazione recognizer: "
            f"{exc}"
        )

        return

    print(
        "[VOLK] Recognizer pronto"
    )

    # ========================================================
    # RECOGNIZER LIBERO (fallback nome timer)
    # ========================================================

    try:

        free_recognizer = create_free_recognizer(
            model
        )

    except Exception as exc:

        print(
            "[VOLK] "
            "ERRORE creazione recognizer libero: "
            f"{exc}"
        )

        return

    # ========================================================
    # READY
    # ========================================================

    if ready_event is not None:

        ready_event.set()

    print(
        "[VOLK] "
        "Pronto all'ascolto"
    )

    # ========================================================
    # MAIN LOOP
    # ========================================================

    while not stop_event.is_set():

        try:

            audio_buffer, sample_rate = (
                audio_queue.get(
                    timeout=1
                )
            )

        except Empty:

            continue

        except Exception as exc:

            print(
                "[VOLK] "
                "Errore lettura audio_queue: "
                f"{exc}"
            )

            continue

        try:

            # =================================================
            # EMPTY BUFFER
            # =================================================

            if not audio_buffer:

                print(
                    "[VOLK] Buffer audio vuoto"
                )

                continue

            # =================================================
            # AUDIO PREPARATION
            # =================================================

            audio_array = prepare_audio(
                audio_buffer,
                sample_rate
            )

            if audio_array.size == 0:

                print(
                    "[VOLK] Audio vuoto "
                    "dopo preparazione"
                )

                continue

            duration = (
                len(audio_array)
                / VOSK_SAMPLE_RATE
            )

            print(
                "[VOLK] "
                f"Elaborazione audio: "
                f"{duration:.2f}s"
            )

            # =================================================
            # PCM
            # =================================================

            pcm_bytes = (
                audio_array.tobytes()
            )

            # =================================================
            # RESET RECOGNIZER
            # =================================================

            recognizer.Reset()

            # =================================================
            # DECODE
            # =================================================

            start_time = (
                time.monotonic()
            )

            recognizer.AcceptWaveform(
                pcm_bytes
            )

            text = get_final_text(
                recognizer
            )

            elapsed = (
                time.monotonic()
                - start_time
            )

            # =================================================
            # EMPTY RESULT
            # =================================================

            if not text:

                print(
                    "[VOLK] "
                    "Nessun comando riconosciuto "
                    f"[decode: "
                    f"{elapsed:.3f}s]"
                )

                continue

            # =================================================
            # FALLBACK DETTATURA LIBERA (nome timer)
            # =================================================
            #
            # Attivato quando la grammatica chiusa non ha
            # riconosciuto nulla di utile (tutto [unk]) oppure
            # quando la parola "timer" compare comunque nel
            # risultato: in entrambi i casi il nome libero del
            # timer, se presente, è andato perso come [unk] e va
            # recuperato con una seconda passata senza grammatica
            # sullo stesso audio.

            necessita_fallback = (
                text == UNKNOWN_TOKEN
                or UNKNOWN_TOKEN in text
                or "timer" in text.split()
            )

            if necessita_fallback:

                free_recognizer.Reset()

                free_recognizer.AcceptWaveform(
                    pcm_bytes
                )

                free_text = get_final_text(
                    free_recognizer
                )

                if "timer" in free_text.split():

                    print(
                        "[VOLK] "
                        "Fallback dettatura libera (timer): "
                        f"{free_text}"
                    )

                    text = free_text

                elif text == UNKNOWN_TOKEN:

                    print(
                        "[VOLK] "
                        "Audio fuori grammatica "
                        f"[decode: "
                        f"{elapsed:.3f}s]"
                    )

                    continue

                elif UNKNOWN_TOKEN in text:

                    print(
                        "[VOLK] "
                        "Comando parzialmente sconosciuto: "
                        f"{text}"
                    )

                    text = text.replace(
                        UNKNOWN_TOKEN,
                        ""
                    ).strip()

                    if not text:
                        continue

            # =================================================
            # FINAL COMMAND
            # =================================================

            print(
                "[VOLK] "
                "Comando finale trascritto: "
                f"{text} "
                "[Vosk decode time: "
                f"{elapsed:.3f}s]"
            )

            # =================================================
            # SEND TO FUZZY
            # =================================================

            if command_queue is not None:

                command_queue.put(
                    text
                )

                print(
                    "[VOLK] "
                    "Comando inviato "
                    "al fuzzy parser"
                )

        except json.JSONDecodeError as exc:

            print(
                "[VOLK] "
                "ERRORE parsing JSON Vosk: "
                f"{exc}"
            )

        except Exception as exc:

            print(
                "[VOLK] "
                "ERRORE riconoscimento: "
                f"{exc}"
            )

        finally:

            try:
                audio_queue.task_done()

            except Exception:
                pass

    # ========================================================
    # STOP
    # ========================================================

    print(
        "[VOLK] Thread terminato"
    )