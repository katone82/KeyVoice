import os
import subprocess
import tempfile
import threading

# ============================================================
# SUONI DI CONFERMA COMANDO
# ============================================================
#
# Stesso principio del beep di wake word in
# xvf3800_wakeword_listener.py (play_beep), ma per il RISULTATO
# del comando dopo che e' stato trascritto e interpretato, come
# fanno Alexa/Google Home:
#
#   play_command_ok()    -> comando capito ED eseguito con
#                            successo su Home Assistant
#   play_command_error() -> comando NON eseguito, per uno di
#                            questi motivi:
#                              - non riconosciuto dal fuzzy
#                                parser (azione/entita' non
#                                trovate) -> fuzzy_parser.py,
#                                processa_comandi()
#                              - riconosciuto ma Home Assistant
#                                ha risposto errore, non ha
#                                risposto, o l'azione non e'
#                                ancora gestita -> run_service.py,
#                                invia_comando_ha()
#
# Modulo separato (invece di importare le costanti da
# xvf3800_wakeword_listener.py) cosi' il thread fuzzy/HA non
# dipende dall'inizializzazione hardware del wake word listener
# (che al primo import valida la presenza di xvf_host.py /
# xvf3800-tool).

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

COMMAND_OK_FILE = os.path.join(
    SCRIPT_DIR, "sounds", "command_ok.wav"
)

COMMAND_ERROR_FILE = os.path.join(
    SCRIPT_DIR, "sounds", "command_error.wav"
)

TIMER_ALARM_FILE = os.path.join(
    SCRIPT_DIR, "sounds", "timer_finished.wav"
)

# Il file va scelto/registrato con pause di silenzio già presenti
# al suo interno (es. un rintocco che decade naturalmente prima
# del prossimo giro di loop) — vedi _run_alarm_loop(), che lo
# riproduce semplicemente in loop senza aggiungere pause di
# codice. Senza silenzio reale tra un rintocco e l'altro, non c'è
# mai un momento in cui il wake word listener possa sentire
# "spegni timer" (per fermare la suoneria serve la voce, quindi
# un file senza pause crea un blocco circolare).
#
# sounds/timer_finished.wav attuale: "Marimba Bloop 1" di
# floraphonic (pixabay.com/sound-effects/film-special-effects-
# marimba-bloop-1-188150/), Pixabay Content License (uso libero
# personale/commerciale, nessuna attribuzione richiesta) —
# riconvertito da MP3 a WAV mono 48kHz/16-bit.

# Stessa scheda ALSA del beep di wake word (BEEP_DEVICE in
# xvf3800_wakeword_listener.py). Se cambi scheda audio, aggiorna
# anche qui oppure sovrascrivi da config.json (vedi apply_config
# sotto).
COMMAND_SOUND_DEVICE = "plughw:3,0"

COMMAND_FEEDBACK_ENABLED = True

# Sintesi vocale (annunci dinamici, es. "timer di dieci minuti
# creato"). Due motori intercambiabili, scelti da TTS_ENGINE:
#
#   "espeak" (default) -> espeak-ng, robotico ma zero setup
#                          extra (sudo apt install espeak-ng)
#   "piper"             -> voce neurale molto più naturale,
#                          resta leggero (pensato per Pi), ma
#                          richiede pip install piper-tts + il
#                          download di un modello voce .onnx
#                          (vedi services/README.md)
TTS_ENGINE = "espeak"

TTS_VOCE = "it"
TTS_VELOCITA = 150

PIPER_BINARY = "piper"
PIPER_MODEL = ""


# ============================================================
# CONFIG DA config.json (opzionale)
# ============================================================

def apply_config(cfg):
    """
    Sovrascrive le costanti di modulo con eventuali override
    dalla sezione "command_feedback" di config.json. Chiavi
    assenti = restano i default hardcoded qui sopra.

    Esempio in config.json:

        "command_feedback": {
            "enabled": true,
            "device": "plughw:3,0",
            "ok_file": "./sounds/command_ok.wav",
            "error_file": "./sounds/command_error.wav",
            "timer_alarm_file": "./sounds/timer_finished.wav"
        }
    """

    global COMMAND_OK_FILE, COMMAND_ERROR_FILE
    global TIMER_ALARM_FILE
    global COMMAND_SOUND_DEVICE, COMMAND_FEEDBACK_ENABLED
    global TTS_ENGINE, TTS_VOCE, TTS_VELOCITA
    global PIPER_BINARY, PIPER_MODEL

    if not cfg:
        return

    COMMAND_FEEDBACK_ENABLED = cfg.get(
        "enabled", COMMAND_FEEDBACK_ENABLED
    )

    COMMAND_SOUND_DEVICE = cfg.get(
        "device", COMMAND_SOUND_DEVICE
    )

    def _resolve(path_override):
        if os.path.isabs(path_override):
            return path_override
        return os.path.abspath(
            os.path.join(SCRIPT_DIR, path_override)
        )

    ok_override = cfg.get("ok_file")

    if ok_override:
        COMMAND_OK_FILE = _resolve(ok_override)

    error_override = cfg.get("error_file")

    if error_override:
        COMMAND_ERROR_FILE = _resolve(error_override)

    alarm_override = cfg.get("timer_alarm_file")

    if alarm_override:
        TIMER_ALARM_FILE = _resolve(alarm_override)

    TTS_ENGINE = cfg.get(
        "tts_engine", TTS_ENGINE
    )

    TTS_VOCE = cfg.get(
        "tts_voice", TTS_VOCE
    )

    TTS_VELOCITA = cfg.get(
        "tts_speed", TTS_VELOCITA
    )

    PIPER_BINARY = cfg.get(
        "piper_binary", PIPER_BINARY
    )

    piper_model_override = cfg.get("piper_model")

    if piper_model_override:
        PIPER_MODEL = _resolve(piper_model_override)

    if (
        TTS_ENGINE == "piper"
        and not (PIPER_MODEL and os.path.isfile(PIPER_MODEL))
    ):
        print(
            "[SOUND] ATTENZIONE: tts_engine=piper ma "
            "piper_model non trovato: "
            f"{PIPER_MODEL or '(non impostato)'}"
        )

    for label, path in (
        ("ok_file", COMMAND_OK_FILE),
        ("error_file", COMMAND_ERROR_FILE),
        ("timer_alarm_file", TIMER_ALARM_FILE),
    ):
        if not os.path.isfile(path):
            print(
                f"[SOUND] ATTENZIONE: {label} da config.json "
                f"non trovato: {path}"
            )

    print(
        "[SOUND] Configurazione feedback comando applicata "
        f"(enabled={COMMAND_FEEDBACK_ENABLED}, "
        f"device={COMMAND_SOUND_DEVICE})"
    )


# ============================================================
# RIPRODUZIONE
# ============================================================

def _play_one(path: str, label: str) -> bool:
    """
    Riproduce un singolo file (bloccante: aplay -D ... termina
    da solo a fine file). Ritorna False se il file manca o aplay
    fallisce, solo per logging da parte del chiamante.
    """

    if not os.path.isfile(path):
        print(f"[SOUND] File non trovato ({label}): {path}")
        return False

    try:
        result = subprocess.run(
            [
                "aplay",
                "-q",
                "-D",
                COMMAND_SOUND_DEVICE,
                path
            ],
            capture_output=True,
            text=True,
            timeout=5.0
        )

        if result.returncode != 0:
            print(
                f"[SOUND] aplay fallito ({label}, "
                f"device={COMMAND_SOUND_DEVICE}, "
                f"file={path}): {result.stderr.strip()}"
            )
            return False

    except Exception as exc:
        print(f"[SOUND] Errore riproduzione {label}: {exc}")
        return False

    return True


def _play_sequence(paths, label: str) -> None:
    """
    Riproduce piu' file in sequenza (uno dopo l'altro, aspettando
    che ciascuno finisca) su un thread separato, per non bloccare
    il chiamante (thread fuzzy/HA) per la durata della
    riproduzione. Stessa logica di base di play_beep() in
    xvf3800_wakeword_listener.py, estesa a piu' file.
    """

    if not COMMAND_FEEDBACK_ENABLED:
        return

    def _run():
        for path in paths:
            _play_one(path, label)

    threading.Thread(target=_run, daemon=True).start()


def play_command_ok() -> None:
    """
    Comando capito ED eseguito con successo su Home Assistant.
    Solo command_ok.wav: il beep di wake word (wake.wav) resta
    di competenza esclusiva di xvf3800_wakeword_listener.py al
    momento del rilevamento della wake word, non va ripetuto qui.
    """
    _play_sequence([COMMAND_OK_FILE], "command_ok")


def play_command_error() -> None:
    """
    Comando NON eseguito: non riconosciuto dal fuzzy parser,
    oppure riconosciuto ma fallito lato Home Assistant.
    """
    _play_sequence([COMMAND_ERROR_FILE], "command_error")


# ============================================================
# SUONERIA FINE TIMER (loop finché non arriva lo stop)
# ============================================================
#
# A differenza di play_command_ok/error (un suono e via), la
# fine di un timer deve restare udibile finché l'utente non
# dice "spegni timer" — altrimenti un trillo di 1-2 secondi si
# perde facilmente. TIMER_ALARM_FILE viene quindi rimandato in
# loop da un thread dedicato, interrotto da stop_timer_alarm()
# (fuzzy_parser.py, timer_command_consumer(), comando
# "cancella"/"spegni").

_alarm_thread = None
_alarm_process = None
_alarm_stop_event = threading.Event()
_alarm_lock = threading.Lock()


def play_timer_alarm() -> None:
    """
    Avvia la suoneria di fine timer in loop. Se è già in
    riproduzione (es. più timer scaduti quasi insieme), non fa
    nulla: resta un solo loop attivo.
    """

    global _alarm_thread

    if not COMMAND_FEEDBACK_ENABLED:
        return

    with _alarm_lock:
        if _alarm_thread is not None and _alarm_thread.is_alive():
            return

        _alarm_stop_event.clear()

        _alarm_thread = threading.Thread(
            target=_run_alarm_loop, daemon=True
        )
        _alarm_thread.start()


def stop_timer_alarm() -> bool:
    """
    Ferma subito la suoneria di fine timer, se in corso
    (termina anche la riproduzione aplay già avviata, non
    aspetta la fine del file). Restituisce True se ha
    effettivamente fermato qualcosa, False se non stava
    suonando nulla — usato da fuzzy_parser.py per distinguere
    "suoneria spenta" da "nessun timer da cancellare".
    """

    with _alarm_lock:
        era_attiva = (
            _alarm_thread is not None
            and _alarm_thread.is_alive()
        )

        _alarm_stop_event.set()

        if _alarm_process is not None:
            try:
                _alarm_process.terminate()
            except Exception:
                pass

    return era_attiva


def _run_alarm_loop() -> None:
    global _alarm_process

    if not os.path.isfile(TIMER_ALARM_FILE):
        print(
            "[SOUND] File non trovato (timer_alarm): "
            f"{TIMER_ALARM_FILE}"
        )
        return

    while not _alarm_stop_event.is_set():
        try:
            with _alarm_lock:
                if _alarm_stop_event.is_set():
                    return

                _alarm_process = subprocess.Popen(
                    [
                        "aplay",
                        "-q",
                        "-D",
                        COMMAND_SOUND_DEVICE,
                        TIMER_ALARM_FILE,
                    ],
                )

            _alarm_process.wait()

        except Exception as exc:
            print(
                f"[SOUND] Errore riproduzione timer_alarm: {exc}"
            )
            return

        finally:
            with _alarm_lock:
                _alarm_process = None


# ============================================================
# SINTESI VOCALE (annunci dinamici)
# ============================================================

def _sintetizza_espeak(
    testo: str,
    tmp_path: str
) -> bool:
    risultato = subprocess.run(
        [
            "espeak-ng",
            "-v", TTS_VOCE,
            "-s", str(TTS_VELOCITA),
            "-w", tmp_path,
            testo,
        ],
        capture_output=True,
        text=True,
        timeout=10.0,
    )

    if risultato.returncode != 0:
        print(
            "[SOUND] espeak-ng fallito: "
            f"{risultato.stderr.strip()}"
        )
        return False

    return True


def _sintetizza_piper(
    testo: str,
    tmp_path: str
) -> bool:
    if not PIPER_MODEL:
        print(
            "[SOUND] tts_engine=piper ma piper_model non "
            "configurato in config.json "
            "(command_feedback.piper_model)"
        )
        return False

    risultato = subprocess.run(
        [
            PIPER_BINARY,
            "--model", PIPER_MODEL,
            "--output_file", tmp_path,
        ],
        input=testo,
        capture_output=True,
        text=True,
        timeout=15.0,
    )

    if risultato.returncode != 0:
        print(
            "[SOUND] piper fallito: "
            f"{risultato.stderr.strip()}"
        )
        return False

    return True


def speak(
    testo: str
) -> None:
    """
    Sintetizza e riproduce una frase (es. "timer di dieci
    minuti creato"). A differenza dei wav fissi sopra, il
    contenuto è dinamico, quindi non può essere pre-registrato
    — va generato al volo, in un thread separato per non
    bloccare il chiamante (stesso principio di _play_sequence).

    Motore scelto da TTS_ENGINE ("espeak" di default, oppure
    "piper" per una voce neurale più naturale — vedi il
    commento sulle costanti di modulo più sopra).
    """

    if not COMMAND_FEEDBACK_ENABLED:
        return

    if not testo:
        return

    def _run():
        tmp_path = None

        try:
            descrittore, tmp_path = tempfile.mkstemp(
                suffix=".wav",
                prefix="keyvoice_tts_",
            )

            os.close(descrittore)

            if TTS_ENGINE == "piper":
                ok = _sintetizza_piper(
                    testo, tmp_path
                )
            else:
                ok = _sintetizza_espeak(
                    testo, tmp_path
                )

            if ok:
                _play_one(tmp_path, "tts")

        except FileNotFoundError:
            comando = (
                PIPER_BINARY
                if TTS_ENGINE == "piper"
                else "espeak-ng"
            )

            print(
                f"[SOUND] {comando} non trovato "
                f"(motore TTS configurato: {TTS_ENGINE})"
            )

        except Exception as exc:
            print(
                f"[SOUND] Errore sintesi vocale: {exc}"
            )

        finally:
            if tmp_path and os.path.isfile(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    threading.Thread(
        target=_run, daemon=True
    ).start()
