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

    ok_override = cfg.get("ok_file")

    if ok_override:
        if os.path.isabs(ok_override):
            COMMAND_OK_FILE = ok_override
        else:
            COMMAND_OK_FILE = os.path.abspath(
                os.path.join(SCRIPT_DIR, ok_override)
            )

    error_override = cfg.get("error_file")

    if error_override:
        if os.path.isabs(error_override):
            COMMAND_ERROR_FILE = error_override
        else:
            COMMAND_ERROR_FILE = os.path.abspath(
                os.path.join(SCRIPT_DIR, error_override)
            )

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

def _play(path: str, label: str) -> None:

    if not COMMAND_FEEDBACK_ENABLED:
        return

    if not os.path.isfile(path):
        print(f"[SOUND] File non trovato ({label}): {path}")
        return

    def _run():
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

        except Exception as exc:
            print(f"[SOUND] Errore riproduzione {label}: {exc}")

    # Thread separato per non bloccare il chiamante (thread
    # fuzzy/HA) per la durata della riproduzione, stessa logica
    # di play_beep() in xvf3800_wakeword_listener.py.
    threading.Thread(target=_run, daemon=True).start()


def play_command_ok() -> None:
    """Comando capito ED eseguito con successo su Home Assistant."""
    _play(COMMAND_OK_FILE, "command_ok")


def play_command_error() -> None:
    """
    Comando NON eseguito: non riconosciuto dal fuzzy parser,
    oppure riconosciuto ma fallito lato Home Assistant.
    """
    _play(COMMAND_ERROR_FILE, "command_error")
