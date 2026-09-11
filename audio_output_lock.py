import threading

# ============================================================
# LOCK CONDIVISO PER LA RIPRODUZIONE AUDIO
# ============================================================
#
# Beep di wake word (xvf3800_wakeword_listener.py) e suoni di
# feedback/suoneria timer/TTS (sound_feedback.py) girano in
# thread indipendenti ma riproducono tutti sullo stesso device
# ALSA hardware (es. "plughw:3,0"). Un device "plughw" diretto
# (a differenza di "dmix"/PulseAudio) permette una sola
# riproduzione aperta alla volta: se due `aplay` provano ad
# aprirlo nello stesso istante, il secondo fallisce con
# "Device or resource busy" invece di aspettare.
#
# Modulo minimo e senza altre dipendenze (come debug_config.py)
# apposta per essere importabile da entrambi senza creare un
# ciclo di import: ogni chiamata ad aplay va fatta con
#
#     with audio_output_lock.PLAYBACK_LOCK:
#         subprocess.run(["aplay", ...])
#
# così le riproduzioni si accodano invece di scontrarsi. Il
# lock va tenuto SOLO per la durata di una singola invocazione
# di aplay (un trillo, un beep, una frase TTS), non per un
# intero loop (es. la suoneria del timer): altrimenti un suono
# lungo/ripetuto bloccherebbe qualunque altro suono per tutta
# la sua durata invece di lasciare spazio tra una ripetizione e
# l'altra.
PLAYBACK_LOCK = threading.Lock()


# ============================================================
# REGISTRO DEI PROCESSI aplay ATTIVI
# ============================================================
#
# Permette di interrompere IMMEDIATAMENTE qualunque riproduzione
# in corso (es. quando scatta la wake word: il comando che segue
# ha priorità su un suono già avviato, non ha senso aspettare che
# finisca da solo). Ogni chiamante che lancia un aplay con
# subprocess.Popen si registra qui subito dopo l'avvio e si
# de-registra alla fine (successo, errore o già interrotto):
#
#     proc = subprocess.Popen(["aplay", ...], ...)
#     register(proc)
#     try:
#         proc.communicate(timeout=...)
#     finally:
#         unregister(proc)
#
# stop_all() termina tutti i processi correntemente registrati.
# Non ferma da sola un loop che ne rilancia uno subito dopo (es.
# la suoneria del timer): quel caso va gestito dal chiamante
# fermando anche il proprio ciclo (vedi sound_feedback.
# stop_all_playback()).

_active_processes = []
_registry_lock = threading.Lock()


def register(proc):
    with _registry_lock:
        _active_processes.append(proc)


def unregister(proc):
    with _registry_lock:
        if proc in _active_processes:
            _active_processes.remove(proc)


def stop_all():
    """
    Termina (SIGTERM) ogni processo aplay attualmente registrato.
    Innocuo se non c'è nulla in riproduzione (lista vuota) o se un
    processo è già terminato da solo nel frattempo.
    """

    with _registry_lock:
        procs = list(_active_processes)

    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            pass
