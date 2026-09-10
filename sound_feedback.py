import os
import subprocess
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

# Stessa scheda ALSA del beep di wake word (BEEP_DEVICE in
# xvf3800_wakeword_listener.py). Se cambi scheda audio, aggiorna
# anche qui oppure sovrascrivi da config.json (vedi apply_config
# sotto).
COMMAND_SOUND_DEVICE = "plughw:3,0"

COMMAND_FEEDBACK_ENABLED = True


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
            "error_file": "./sounds/command_error.wav"
        }
    """

    global COMMAND_OK_FILE, COMMAND_ERROR_FILE
    global COMMAND_SOUND_DEVICE, COMMAND_FEEDBACK_ENABLED

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

    for label, path in (
        ("ok_file", COMMAND_OK_FILE),
        ("error_file", COMMAND_ERROR_FILE),
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
