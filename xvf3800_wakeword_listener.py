import os
import sys
import time
import threading
import queue
import subprocess
import collections
import wave
import math
import re

import numpy as np
import sounddevice as sd
from openwakeword.model import Model


# ============================================================
# CONFIGURAZIONE
# ============================================================

# Log dettagliati (telemetria XVF ad ogni poll, score wake word
# periodico, riga RMS/direzione ad ogni blocco durante la
# registrazione). Lascia False per vedere solo i punti cardine
# della pipeline (wake, direzione, inizio/fine comando, salvataggio
# WAV). Metti a True solo quando serve ritarare soglie o
# diagnosticare un problema.
DEBUG_LOGGING = False

TARGET_SAMPLE_RATE = 16000
DEVICE_SAMPLE_RATE = 16000
CHANNELS = 2

# Canale usato SOLO per la wake word (RMS/direzione continuano a
# usare il canale 0). Se il canale 1 del reSpeaker porta un
# segnale processato diversamente (es. output con AEC/NS attivi
# invece del raw), può dare uno score più alto a distanza. Prova
# a metterlo a 1 e confronta gli score in [WAKE-DEBUG] a parità
# di distanza/voce.
WAKE_AUDIO_CHANNEL = 0

WAKEWORD = "hey_jarvis"

# ------------------------------------------------------------
# Soglia a due livelli
# ------------------------------------------------------------
# Un singolo chunk molto sicuro scatta subito (WAKE_THRESHOLD_HIGH).
# Un chunk meno sicuro (parlato più debole/distante/mugugnato)
# richiede conferma su più chunk consecutivi sopra una soglia più
# bassa (WAKE_THRESHOLD_LOW + WAKE_CONFIRM_CHUNKS): cattura più
# pronunce reali senza abbassare la soglia unica e aumentare i
# falsi positivi su rumore.
# Ritara osservando i valori reali negli score [WAKE-DEBUG].
WAKE_THRESHOLD_HIGH = 0.45
WAKE_THRESHOLD_LOW = 0.15
WAKE_CONFIRM_CHUNKS = 3

# openWakeWord è progettato per ricevere audio in blocchi da
# esattamente 80ms (1280 campioni a 16kHz): è la dimensione con
# cui la sua pipeline di melspectrogram + embedding è allineata.
# Il resto del sistema usa BLOCK_SIZE (30ms) per la granularità
# del gate RMS/direzionale, quindi bufferizziamo l'audio e lo
# passiamo al modello di wake word in blocchi separati da questa
# dimensione, invece di alimentarlo con chunk da 30ms non
# allineati (che abbassano il punteggio di picco raggiunto e
# costringono a ripetere la wake word più volte).
WAKE_CHUNK_SAMPLES = 1280

# NOTA: openWakeWord mantiene un proprio buffer streaming
# interno tra una chiamata a predict() e l'altra (calcola le
# feature audio in modo incrementale sulla sequenza che gli
# viene passata). Alimentarlo con finestre sovrapposte (overlap
# manuale) reinserisce gli stessi campioni due volte nel suo
# stream interno, disallineando la sua timeline e azzerando lo
# score. Il chunk successivo deve quindi partire esattamente
# dove finisce il precedente: nessun overlap, hop = chunk size.
WAKE_HOP_SAMPLES = WAKE_CHUNK_SAMPLES

# Battito cardiaco: una riga leggera ogni tot secondi mentre si
# è in ascolto, per confermare che il processo è vivo anche con
# DEBUG_LOGGING spento (utile per distinguere "in ascolto in
# silenzio" da "bloccato per davvero").
ALIVE_LOG_INTERVAL = 15.0

# Stampa lo score della wake word anche quando resta sotto
# soglia, per poter tarare WAKE_THRESHOLD_HIGH/LOW osservando i
# valori reali durante l'uso.
WAKE_DEBUG_INTERVAL = 1.0

BLOCK_MS = 30
BLOCK_SIZE = int(DEVICE_SAMPLE_RATE * BLOCK_MS / 1000)

# ------------------------------------------------------------
# Speech gate RMS
# ------------------------------------------------------------

SPEECH_START_RATIO = 2.5
SPEECH_END_RATIO = 1.5

# Con la direzione già bloccata dalla wake word, la sorgente è
# considerata attendibile fin da subito: basta un solo blocco di
# conferma (~1 ciclo audio) invece dei 0.24s precedenti, che
# tagliavano l'inizio della parola. Non azzerare del tutto per
# evitare che un singolo blocco rumoroso avvii una registrazione.
SPEECH_START_TIME = 0.03

# Pausa "concreta" richiesta dalla stessa direzione prima di
# considerare finito il comando. Leggermente piu' alta di prima
# per non tagliare respiri o micro-pause naturali nel parlato.
SPEECH_END_TIME = 0.60

NOISE_CALIBRATION_SECONDS = 2.0
NOISE_MIN_FLOOR = 50.0
NOISE_UPDATE_ALPHA = 0.02

# ------------------------------------------------------------
# Wake / comando
# ------------------------------------------------------------

POST_WAKE_IGNORE_SECONDS = 0.40

COMMAND_TIMEOUT_SECONDS = 4.0
MAX_COMMAND_SECONDS = 6.0

# Con la direzione bloccata dalla wake word e la conferma
# vocale quasi istantanea (SPEECH_START_TIME), non serve piu'
# un pre-roll lungo per compensare il ritardo di conferma.
# Lo teniamo comunque a mezzo secondo come cuscinetto per
# l'attacco morbido della voce (il volume sale gradualmente
# prima di superare SPEECH_START_RATIO).
PRE_ROLL_SECONDS = 0.50

# ------------------------------------------------------------
# XVF3800
# ------------------------------------------------------------

XVF_HOST_PATH = "./xvf3800-tool/vendor/xvf_host.py"

XVF_POLL_INTERVAL = 0.10

# NOTA: prima di questo fix il rapporto di dominanza era quasi
# sempre ~1.0 perche' uno dei beam riportati dal device
# duplicava esattamente il beam dominante (vedi fix in
# XVF3800Telemetry._worker). Con la deduplica il rapporto reale
# osservato in ambiente tipico e' spesso 1.1-1.3x: la soglia va
# quindi ritarata sul campo guardando i nuovi log [XVF].
DIRECTION_MIN_DOMINANCE = 1.2
DIRECTION_MIN_ENERGY = 1.0

DIRECTION_CONFIRMATIONS = 3

# NOTA affidabilità: in ambienti riverberanti la direzione
# stimata dalla telemetria XVF può "saltare" anche di parecchie
# decine di gradi sulla STESSA sorgente (riflessioni sui muri),
# non solo quando cambia davvero chi sta parlando. Con valori
# troppo stretti qui, process_recording() zittisce (sostituisce
# con silenzio) blocchi di parlato reale a metà comando, "bucando"
# l'audio inviato a Vosk e peggiorando il riconoscimento anche
# quando la wake word e la voce erano perfettamente chiare.
DIRECTION_ANGLE_TOLERANCE = 60.0

DIRECTION_LOST_GRACE_SECONDS = 0.5

# Quando scatta la wake word, cerchiamo nello storico telemetria
# la lettura di direzione più energica negli ultimi N secondi:
# copre la coda dell'utterance "hey jarvis" (l'engine di wake
# word finalizza il riconoscimento con un piccolo ritardo dopo
# che la frase è stata pronunciata). Allargata da 0.6 a 1.0s: la
# telemetria XVF ha spesso letture a energia zero anche durante
# il parlato (poll ogni 100ms, non sincronizzato con la voce),
# quindi una finestra più ampia aumenta le probabilità di trovare
# almeno una lettura valida ed evitare il fallback più lento.
WAKE_DIRECTION_LOOKBACK_SECONDS = 1.0

# Quanta storia di telemetria conserviamo per il lookback sopra.
XVF_HISTORY_SECONDS = 2.0

# Stampa diagnostica telemetria ogni N secondi
XVF_DEBUG_INTERVAL = 0.50

# Silenzia i log molto verbosi "ReadCMD: ..." generati
# internamente da xvf_host.py. Metti a False per riattivarli
# in fase di debug del protocollo USB.
XVF_SILENCE_VENDOR_LOGS = True

# ------------------------------------------------------------
# Audio
# ------------------------------------------------------------

INPUT_DEVICE_NAME = "reSpeaker XVF3800 4-Mic Array"

COMMAND_WAV = "/tmp/keyvoice_command.wav"

# Percorso relativo alla cartella del progetto, come xvf_host.py
# poco sotto. Verrà risolto in assoluto subito dopo.
BEEP_FILE = "./sounds/wake.wav"
BEEP_DEVICE = "plughw:3,0"


# ============================================================
# PATH XVF
# ============================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

XVF_HOST_PATH = os.path.abspath(
    os.path.join(SCRIPT_DIR, XVF_HOST_PATH)
)

if not os.path.isfile(XVF_HOST_PATH):
    raise FileNotFoundError(
        f"xvf_host.py non trovato: {XVF_HOST_PATH}"
    )

XVF_HOST_DIR = os.path.dirname(XVF_HOST_PATH)

if XVF_HOST_DIR not in sys.path:
    sys.path.insert(0, XVF_HOST_DIR)

try:
    import xvf_host
except Exception as e:
    print(
        f"[XVF] Impossibile importare xvf_host.py "
        f"da {XVF_HOST_PATH}: {e}"
    )
    raise

if XVF_SILENCE_VENDOR_LOGS:
    # xvf_host.py chiama print() senza qualificatore: assegnando
    # 'print' nel namespace del modulo, ogni print() interno a
    # xvf_host risolve su questo no-op invece che sul builtin.
    xvf_host.print = lambda *args, **kwargs: None


# ============================================================
# PATH BEEP
# ============================================================

BEEP_FILE = os.path.abspath(
    os.path.join(SCRIPT_DIR, BEEP_FILE)
)

if not os.path.isfile(BEEP_FILE):
    print(
        f"[BEEP] ATTENZIONE: file beep non trovato: "
        f"{BEEP_FILE} — il riscontro sonoro alla wake word "
        f"non verrà riprodotto finché il file non è presente."
    )


# ============================================================
# UTILITY
# ============================================================

def normalize_angle(angle):
    while angle < 0:
        angle += 360.0

    while angle >= 360.0:
        angle -= 360.0

    return angle


def angle_distance(a, b):
    d = abs(a - b)

    if d > 180.0:
        d = 360.0 - d

    return d


# ------------------------------------------------------------
# Parametri per il guadagno automatico applicato ai chunk prima
# della wake word (vedi normalize_wake_gain sotto).
# ------------------------------------------------------------

WAKE_AGC_TARGET_PEAK = 0.5

# Guadagno massimo applicabile. Più alto = più portata per il
# parlato debole/distante, ma amplifica di più anche il rumore
# di fondo genuino (rischio di più falsi positivi in ambienti
# rumorosi). Ritara guardando gli score in [WAKE-DEBUG].
WAKE_AGC_MAX_GAIN = 8.0

# Sotto questo picco consideriamo il chunk "silenzio" e non lo
# amplifichiamo (eviterebbe solo di alzare rumore/hiss). ATTENZIONE:
# un valore troppo alto qui tratta come "silenzio" anche parlato
# reale ma debole (voce da 2+ metri può avere picchi ben sotto 50
# su scala int16), azzerando il beneficio dell'AGC proprio quando
# servirebbe di più. Tenerlo basso.
WAKE_AGC_SILENCE_FLOOR = 15


def normalize_wake_gain(chunk):
    """
    Applica un guadagno automatico leggero al chunk prima di
    passarlo al modello di wake word: se si parla da un po' più
    lontano o a volume basso, il segnale può arrivare sotto il
    livello su cui il modello è stato addestrato, abbassando lo
    score anche per una pronuncia corretta.

    Non amplifica se il chunk è praticamente silenzio/rumore di
    fondo (eviterebbe solo di alzare il rumore stesso), e limita
    il guadagno massimo per non introdurre distorsione o
    amplificare eccessivamente un click/rumore isolato.
    """

    chunk_f = chunk.astype(np.float32)

    peak = float(np.max(np.abs(chunk_f)))

    if peak < WAKE_AGC_SILENCE_FLOOR:
        return chunk, peak, 1.0

    target = 32767.0 * WAKE_AGC_TARGET_PEAK

    gain = min(
        target / peak,
        WAKE_AGC_MAX_GAIN
    )

    if gain <= 1.0:
        return chunk, peak, 1.0

    boosted = np.clip(
        chunk_f * gain,
        -32768,
        32767
    )

    return boosted.astype(np.int16), peak, gain


# ============================================================
# XVF3800 TELEMETRY
# ============================================================

class XVF3800Telemetry:

    def __init__(self):

        self.running = False
        self.thread = None

        self.device = None

        self.lock = threading.Lock()

        self.azimuths = []
        self.energies = []

        self.dominant_beam = None
        self.dominant_angle = None
        self.dominant_energy = 0.0
        self.second_energy = 0.0
        self.dominance_ratio = 0.0

        self.last_update = 0.0

        self.error_count = 0

        self.last_debug = 0.0

        # Storico (timestamp, angolo, energia, ratio, valido) per
        # poter recuperare, al momento della wake word, la
        # direzione dominante di qualche centinaio di ms prima.
        self.history = collections.deque(maxlen=200)

    # --------------------------------------------------------

    def start(self):

        if self.running:
            return

        print("[XVF] Avvio telemetry thread")

        self.running = True

        self.thread = threading.Thread(
            target=self._worker,
            name="xvf-telemetry",
            daemon=True
        )

        self.thread.start()

    # --------------------------------------------------------

    def stop(self):

        if not self.running:
            return

        print("[XVF] Stopping telemetry...")

        self.running = False

        if self.thread is not None:
            self.thread.join(timeout=2.0)

        self.thread = None

        if self.device is not None:

            try:
                self.device.close()
            except Exception:
                pass

            self.device = None

    # --------------------------------------------------------

    def _connect(self):

        print("[XVF] Connessione al dispositivo...")

        self.device = xvf_host.find()

        if not self.device:
            raise RuntimeError(
                "XVF3800 non trovato"
            )

        print("[XVF] Dispositivo connesso")

    # --------------------------------------------------------

    def _read(self):

        if self.device is None:
            self._connect()

        azimuths = self.device.read(
            "AEC_AZIMUTH_VALUES"
        )

        energies = self.device.read(
            "AEC_SPENERGY_VALUES"
        )

        return list(azimuths), list(energies)

    # --------------------------------------------------------

    @staticmethod
    def _dedupe_beams(degrees, energies):
        """
        Alcuni indici riportati dal device (es. l'ultimo slot)
        duplicano semplicemente il beam attualmente dominante
        invece di rappresentare una direzione fisica distinta.
        Se non li scartiamo, il "secondo classificato" per
        energia risulta sempre identico al dominante e il
        rapporto di dominanza resta artificialmente ~1.0,
        impedendo qualunque lock direzionale.

        Ritorna la lista di (indice_originale, angolo, energia)
        con i duplicati (stesso angolo e stessa energia, entro
        una tolleranza minima) collassati in una sola voce.
        """

        seen = set()
        unique = []

        for i, (ang, en) in enumerate(zip(degrees, energies)):

            key = (round(ang, 1), round(en, 1))

            if key in seen:
                continue

            seen.add(key)
            unique.append((i, ang, en))

        return unique

    # --------------------------------------------------------

    def _worker(self):

        while self.running:

            started = time.monotonic()

            try:

                azimuths, energies = self._read()

                if not azimuths or not energies:
                    raise RuntimeError(
                        "Telemetry vuota"
                    )

                count = min(
                    len(azimuths),
                    len(energies)
                )

                azimuths = azimuths[:count]
                energies = energies[:count]

                degrees = [
                    normalize_angle(
                        math.degrees(float(value))
                    )
                    for value in azimuths
                ]

                energies = [
                    float(value)
                    for value in energies
                ]

                unique_beams = self._dedupe_beams(
                    degrees, energies
                )

                if not unique_beams:
                    raise RuntimeError(
                        "Telemetry vuota dopo dedup"
                    )

                dominant, dominant_angle, dominant_energy = max(
                    unique_beams,
                    key=lambda item: item[2]
                )

                other_energies = [
                    en
                    for idx, _, en in unique_beams
                    if idx != dominant
                ]

                second_energy = (
                    max(other_energies)
                    if other_energies
                    else 0.0
                )

                ratio = (
                    dominant_energy /
                    max(second_energy, 1.0)
                )

                valid = (
                    dominant_energy
                    >= DIRECTION_MIN_ENERGY
                    and ratio
                    >= DIRECTION_MIN_DOMINANCE
                )

                with self.lock:

                    self.azimuths = degrees
                    self.energies = energies

                    self.dominant_beam = dominant

                    self.dominant_angle = (
                        dominant_angle
                    )

                    self.dominant_energy = (
                        dominant_energy
                    )

                    self.second_energy = (
                        second_energy
                    )

                    self.dominance_ratio = ratio

                    self.last_update = (
                        time.monotonic()
                    )

                    self.history.append(
                        (
                            self.last_update,
                            dominant_angle,
                            dominant_energy,
                            ratio,
                            valid
                        )
                    )

                # ------------------------------------------------
                # DEBUG TELEMETRIA (solo se DEBUG_LOGGING attivo)
                # ------------------------------------------------

                if DEBUG_LOGGING:

                    now = time.monotonic()

                    if (
                        now - self.last_debug
                        >= XVF_DEBUG_INTERVAL
                    ):

                        self.last_debug = now

                        beam_text = " | ".join(
                            f"B{i}={degrees[i]:6.1f}° "
                            f"E={energies[i]:.0f}"
                            for i in range(count)
                        )

                        print(
                            f"[XVF] {beam_text} | "
                            f"DOM=B{dominant} "
                            f"{dominant_angle:.1f}° | "
                            f"2nd={second_energy:.0f} "
                            f"ratio={ratio:.2f}"
                        )

            except Exception as e:

                self.error_count += 1

                if self.error_count <= 5:

                    print(
                        f"[XVF] Telemetry error: {e}"
                    )

            elapsed = (
                time.monotonic() - started
            )

            sleep_time = max(
                0.01,
                XVF_POLL_INTERVAL - elapsed
            )

            time.sleep(sleep_time)

    # --------------------------------------------------------

    def snapshot(self):

        with self.lock:

            return {
                "azimuths": list(self.azimuths),
                "energies": list(self.energies),

                "dominant_beam":
                    self.dominant_beam,

                "dominant_angle":
                    self.dominant_angle,

                "dominant_energy":
                    self.dominant_energy,

                "second_energy":
                    self.second_energy,

                "dominance_ratio":
                    self.dominance_ratio,

                "last_update":
                    self.last_update
            }

    # --------------------------------------------------------

    def valid_direction(self):

        data = self.snapshot()

        if data["dominant_beam"] is None:
            return False

        if (
            data["dominant_energy"]
            < DIRECTION_MIN_ENERGY
        ):
            return False

        if (
            data["dominance_ratio"]
            < DIRECTION_MIN_DOMINANCE
        ):
            return False

        return True

    # --------------------------------------------------------

    def matches_direction(self, locked_angle):

        data = self.snapshot()

        if data["dominant_angle"] is None:
            return False

        if (
            data["dominant_energy"]
            < DIRECTION_MIN_ENERGY
        ):
            return False

        if (
            data["dominance_ratio"]
            < DIRECTION_MIN_DOMINANCE
        ):
            return False

        distance = angle_distance(
            data["dominant_angle"],
            locked_angle
        )

        return (
            distance
            <= DIRECTION_ANGLE_TOLERANCE
        )

    # --------------------------------------------------------

    def recent_direction(self, window_seconds):
        """
        Cerca nello storico la lettura di direzione valida più
        energica negli ultimi `window_seconds`. Serve a catturare
        la direzione di provenienza della wake word stessa, così
        il comando successivo può essere filtrato fin da subito
        su quella direzione, senza dover ricostruire un nuovo
        lock da zero (che introduce un ritardo e taglia l'inizio
        della frase).

        Ritorna l'angolo (float) oppure None se nello storico non
        c'è nessuna lettura valida nella finestra richiesta.
        """

        now = time.monotonic()

        with self.lock:
            entries = [
                entry
                for entry in self.history
                if now - entry[0] <= window_seconds
            ]

        valid_entries = [
            entry for entry in entries if entry[4]
        ]

        if not valid_entries:
            return None

        # entry = (timestamp, angle, energy, ratio, valid)
        best = max(
            valid_entries,
            key=lambda entry: entry[2]
        )

        return best[1]


# ============================================================
# ADAPTIVE SPEECH GATE
# ============================================================

class AdaptiveSpeechGate:

    def __init__(self):

        self.noise_floor = NOISE_MIN_FLOOR

    # --------------------------------------------------------

    @staticmethod
    def rms(audio):

        if len(audio) == 0:
            return 0.0

        audio = audio.astype(
            np.float32
        )

        return float(
            np.sqrt(
                np.mean(audio * audio)
            )
        )

    # --------------------------------------------------------

    def ratio(self, rms):

        return rms / max(
            self.noise_floor,
            NOISE_MIN_FLOOR
        )

    # --------------------------------------------------------

    def calibrate(self, frames):

        values = []

        for audio in frames:

            rms = self.rms(audio)

            if rms > 0:
                values.append(rms)

        if not values:
            self.noise_floor = NOISE_MIN_FLOOR
            return

        self.noise_floor = max(
            NOISE_MIN_FLOOR,
            float(
                np.percentile(values, 30)
            )
        )

        print(
            f"[CAL] Noise floor iniziale: "
            f"{self.noise_floor:.1f}"
        )

    # --------------------------------------------------------

    def update(self, rms):

        if rms <= 0:
            return

        ratio = self.ratio(rms)

        if ratio < SPEECH_END_RATIO:

            self.noise_floor = (
                (1.0 - NOISE_UPDATE_ALPHA)
                * self.noise_floor
                +
                NOISE_UPDATE_ALPHA
                * rms
            )


# ============================================================
# WAKE WORD LISTENER
# ============================================================

class WakeWordListener:

    LISTENING = "LISTENING"
    WAIT_COMMAND = "WAIT_COMMAND"
    RECORDING = "RECORDING"
    COOLDOWN = "COOLDOWN"

    def __init__(
        self,
        vosk_audio_queue=None,
        stop_event=None
    ):

        # Coda esterna verso cui inoltrare l'audio del comando
        # catturato, nello stesso formato (buffer, sample_rate)
        # che vosk_listener.py si aspetta di ricevere. None in
        # modalità standalone (esecuzione diretta dello script).
        self.vosk_audio_queue = vosk_audio_queue

        # threading.Event condiviso con gli altri thread di
        # run_service.py per uno spegnimento cooperativo. None in
        # modalità standalone: in quel caso resta l'unico modo per
        # uscire Ctrl+C (KeyboardInterrupt), gestito in run().
        self.stop_event = stop_event

        print(
            f"[INIT] XVF host: "
            f"{XVF_HOST_PATH}"
        )

        print(
            "[INIT] Loading OpenWakeWord..."
        )

        self.model = Model(
            wakeword_models=[
                WAKEWORD
            ]
        )

        print(
            "[INIT] OpenWakeWord loaded"
        )

        self.device_index = self._find_audio_device()

        print(
            f"[INIT] Input device: "
            f"{self.device_index}"
        )

        self.print_alsa_capture_diagnostics()

        self.audio_queue = queue.Queue(
            maxsize=100
        )

        self.state = self.LISTENING

        self.speech_gate = (
            AdaptiveSpeechGate()
        )

        self.xvf = XVF3800Telemetry()

        self.pre_roll = collections.deque(
            maxlen=int(
                PRE_ROLL_SECONDS
                * TARGET_SAMPLE_RATE
                / BLOCK_SIZE
            )
        )

        self.command_audio = []

        self.locked_direction = None

        self.direction_candidate = None
        self.direction_confirmations = 0

        self.direction_last_valid = 0.0

        self.wake_time = 0.0

        self.command_start_time = 0.0

        self.speech_start_candidate = None

        self.last_speech_time = None

        self.cooldown_until = 0.0

        self.last_wake_debug = 0.0

        # Buffer per accumulare i campioni (arrivano a blocchi di
        # BLOCK_SIZE, 30ms) e alimentare openWakeWord in chunk
        # correttamente allineati da WAKE_CHUNK_SAMPLES (80ms).
        self.wake_buffer = np.zeros(0, dtype=np.int16)

        # Quanti chunk consecutivi sopra WAKE_THRESHOLD_LOW ma
        # sotto WAKE_THRESHOLD_HIGH abbiamo visto finora (per il
        # trigger a due livelli, vedi check_wakeword).
        self.wake_confirm_count = 0

        # Ultimo score realmente calcolato dal modello (aggiornato
        # solo quando un chunk da 80ms è stato effettivamente
        # processato), usato per il debug periodico.
        self.last_wake_score = 0.0

        # Picco raw (pre-AGC) e guadagno applicato dall'ultimo
        # chunk processato, usati per il debug periodico: dicono
        # se il segnale in ingresso è debole e se l'AGC sta
        # davvero intervenendo.
        self.last_wake_peak = 0.0
        self.last_wake_gain = 1.0

        # Battito cardiaco leggero (una riga ogni ALIVE_LOG_INTERVAL
        # secondi) per confermare che il processo è vivo anche con
        # DEBUG_LOGGING spento, senza inondare la console.
        self.last_alive_print = 0.0

    # ========================================================
    # AUDIO DEVICE
    # ========================================================

    def _find_audio_device(self):

        devices = sd.query_devices()

        for index, device in enumerate(devices):

            name = device["name"]

            if INPUT_DEVICE_NAME.lower() in name.lower():

                print(
                    f"[AUDIO] Found device "
                    f"{index}: {name}"
                )

                # Il nome riportato da sounddevice per i device
                # ALSA include tipicamente "(hw:X,Y)": lo
                # estraiamo per poter interrogare amixer sulla
                # scheda giusta senza doverlo indovinare/
                # hardcodare (varia da macchina a macchina).
                match = re.search(
                    r"hw:(\d+),\d+",
                    name
                )

                self.alsa_card = (
                    match.group(1)
                    if match
                    else None
                )

                return index

        raise RuntimeError(
            "Dispositivo XVF3800 non trovato"
        )

    # ========================================================
    # DIAGNOSTICA ALSA
    # ========================================================

    def print_alsa_capture_diagnostics(self):
        """
        Stampa i controlli ALSA (amixer) della scheda del
        microfono, così è subito visibile se c'è guadagno di
        cattura non sfruttato senza dover aprire un altro
        terminale. Solo diagnostica: non modifica nulla.

        Usa `amixer contents` (interfaccia "raw", stessa di
        `controls`) invece di `amixer sget` (interfaccia
        "simple", nomi spesso diversi da quelli elencati da
        `controls` — su questo device causava query silenziose
        e senza errore visibile).
        """

        if self.alsa_card is None:

            print(
                "[AUDIO] Impossibile determinare la scheda "
                "ALSA dal nome del device, salto la "
                "diagnostica amixer."
            )

            return

        try:

            result = subprocess.run(
                [
                    "amixer",
                    "-c",
                    self.alsa_card,
                    "contents"
                ],
                capture_output=True,
                text=True,
                timeout=5.0
            )

            if result.returncode != 0:

                print(
                    "[AUDIO] amixer non disponibile o "
                    f"errore (scheda {self.alsa_card}): "
                    f"{result.stderr.strip()}"
                )

                return

            # `amixer contents` stampa un blocco per ogni
            # controllo, a partire da una riga "numid=...".
            # Spezziamo l'output in blocchi e teniamo solo
            # quelli il cui header contiene parole chiave utili.
            blocchi = []
            blocco_corrente = []

            for riga in result.stdout.splitlines():

                if riga.startswith("numid="):

                    if blocco_corrente:
                        blocchi.append(blocco_corrente)

                    blocco_corrente = [riga]

                else:

                    blocco_corrente.append(riga)

            if blocco_corrente:
                blocchi.append(blocco_corrente)

            blocchi_utili = [
                blocco
                for blocco in blocchi
                if any(
                    parola in blocco[0]
                    for parola in (
                        "Capture",
                        "Mic",
                        "Gain"
                    )
                )
            ]

            if not blocchi_utili:

                print(
                    "[AUDIO] Nessun controllo di cattura/gain "
                    f"trovato su scheda {self.alsa_card} "
                    "(potrebbe non essere regolabile via "
                    "ALSA su questo dispositivo)."
                )

                return

            print(
                f"[AUDIO] Controlli di cattura su scheda "
                f"{self.alsa_card}:"
            )

            for blocco in blocchi_utili:

                for riga in blocco:

                    print(f"[AUDIO]   {riga.strip()}")

                nome_controllo = self._estrai_nome_controllo(
                    blocco[0]
                )

                if nome_controllo:

                    print(
                        "[AUDIO]   -> per alzare: "
                        f"amixer -c {self.alsa_card} sset "
                        f"'{nome_controllo}' 100%"
                    )

        except Exception as e:

            print(
                f"[AUDIO] Errore diagnostica amixer: {e}"
            )

    @staticmethod
    def _estrai_nome_controllo(riga_amixer):
        """
        Da una riga tipo "numid=5,iface=MIXER,name='Mic Capture
        Volume'" estrae il nome tra apici singoli.
        """

        match = re.search(
            r"name='([^']+)'",
            riga_amixer
        )

        return (
            match.group(1)
            if match
            else None
        )

    # ========================================================
    # CALLBACK
    # ========================================================

    def _audio_callback(
        self,
        indata,
        frames,
        time_info,
        status
    ):

        if status:
            print(
                f"[AUDIO] {status}"
            )

        audio = indata.copy()

        try:
            self.audio_queue.put_nowait(
                audio
            )
        except queue.Full:
            pass

    # ========================================================
    # BEEP
    # ========================================================

    def play_beep(self):

        if not os.path.isfile(BEEP_FILE):

            print(
                f"[BEEP] File non trovato: {BEEP_FILE}"
            )

            return

        def _run():

            try:

                result = subprocess.run(
                    [
                        "aplay",
                        "-q",
                        "-D",
                        BEEP_DEVICE,
                        BEEP_FILE
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5.0
                )

                if result.returncode != 0:

                    print(
                        f"[BEEP] aplay fallito "
                        f"(device={BEEP_DEVICE}, "
                        f"file={BEEP_FILE}): "
                        f"{result.stderr.strip()}"
                    )

            except Exception as e:

                print(
                    f"[BEEP] Errore: {e}"
                )

        # Eseguito in thread separato per non bloccare il loop
        # audio principale per la durata della riproduzione.
        threading.Thread(
            target=_run,
            daemon=True
        ).start()

    # ========================================================
    # NOISE CALIBRATION
    # ========================================================

    def calibrate_noise(self):

        print(
            "[LISTENER] Calibrazione rumore..."
        )

        frames = []

        total_frames = int(
            NOISE_CALIBRATION_SECONDS
            * TARGET_SAMPLE_RATE
            / BLOCK_SIZE
        )

        for _ in range(total_frames):

            try:

                audio = self.audio_queue.get(
                    timeout=1.0
                )

                mono = audio[:, 0]

                frames.append(
                    mono.copy()
                )

            except queue.Empty:
                pass

        self.speech_gate.calibrate(
            frames
        )

    # ========================================================
    # DIRECTION LOCK
    # ========================================================

    def update_direction_lock(self):

        data = self.xvf.snapshot()

        if not self.xvf.valid_direction():

            self.direction_candidate = None
            self.direction_confirmations = 0

            return False

        angle = data["dominant_angle"]

        if self.direction_candidate is None:

            self.direction_candidate = angle
            self.direction_confirmations = 1

            return False

        distance = angle_distance(
            angle,
            self.direction_candidate
        )

        if (
            distance
            <= DIRECTION_ANGLE_TOLERANCE
        ):

            self.direction_confirmations += 1

        else:

            self.direction_candidate = angle
            self.direction_confirmations = 1

        if (
            self.direction_confirmations
            >= DIRECTION_CONFIRMATIONS
        ):

            self.locked_direction = (
                self.direction_candidate
            )

            self.direction_last_valid = (
                time.monotonic()
            )

            print(
                f"[XVF] Direction LOCK: "
                f"{self.locked_direction:.1f}° "
                f"energy="
                f"{data['dominant_energy']:.0f} "
                f"ratio="
                f"{data['dominance_ratio']:.2f}"
            )

            return True

        return False

    # ========================================================

    def direction_is_active(self):

        if self.locked_direction is None:
            return False

        if self.xvf.matches_direction(
            self.locked_direction
        ):

            self.direction_last_valid = (
                time.monotonic()
            )

            return True

        # piccolo grace period per evitare
        # buchi causati dalla telemetria

        if (
            time.monotonic()
            - self.direction_last_valid
            <= DIRECTION_LOST_GRACE_SECONDS
        ):

            return True

        return False

    # ========================================================
    # WAKE WORD
    # ========================================================

    def check_wakeword(self, audio):

        mono = audio[:, WAKE_AUDIO_CHANNEL]

        # Accumuliamo i campioni ricevuti (blocchi da 30ms) e li
        # passiamo al modello solo in chunk da esattamente
        # WAKE_CHUNK_SAMPLES (80ms), la dimensione con cui
        # openWakeWord è allineato internamente. Un singolo
        # blocco da 30ms può generare più chunk pronti (o zero,
        # se non abbiamo ancora accumulato abbastanza campioni).
        self.wake_buffer = np.concatenate(
            (self.wake_buffer, mono)
        )

        wake_detected = False
        best_score = 0.0

        while len(self.wake_buffer) >= WAKE_CHUNK_SAMPLES:

            chunk = self.wake_buffer[:WAKE_CHUNK_SAMPLES]

            self.wake_buffer = (
                self.wake_buffer[WAKE_HOP_SAMPLES:]
            )

            chunk, peak, gain_applicato = normalize_wake_gain(
                chunk
            )

            self.last_wake_peak = peak
            self.last_wake_gain = gain_applicato

            prediction = self.model.predict(
                chunk
            )

            score = prediction.get(
                WAKEWORD,
                0.0
            )

            if score > best_score:
                best_score = score

            self.last_wake_score = score

            # ------------------------------------------------
            # TRIGGER A DUE LIVELLI
            # ------------------------------------------------

            if score >= WAKE_THRESHOLD_HIGH:

                wake_detected = True

                self.wake_confirm_count = 0

            elif score >= WAKE_THRESHOLD_LOW:

                self.wake_confirm_count += 1

                if (
                    self.wake_confirm_count
                    >= WAKE_CONFIRM_CHUNKS
                ):

                    wake_detected = True

                    self.wake_confirm_count = 0

            else:

                self.wake_confirm_count = 0

        now = time.monotonic()

        if (
            DEBUG_LOGGING
            and now - self.last_wake_debug
            >= WAKE_DEBUG_INTERVAL
        ):

            self.last_wake_debug = now

            print(
                f"[WAKE-DEBUG] score={self.last_wake_score:.3f} "
                f"high={WAKE_THRESHOLD_HIGH} "
                f"low={WAKE_THRESHOLD_LOW} "
                f"peak={self.last_wake_peak:.0f} "
                f"gain={self.last_wake_gain:.2f}x"
            )

        return wake_detected, best_score

    # ========================================================
    # WAIT COMMAND
    # ========================================================

    def process_wait_command(
        self,
        audio
    ):

        now = time.monotonic()

        mono = audio[:, 0]

        rms = self.speech_gate.rms(
            mono
        )

        ratio = self.speech_gate.ratio(
            rms
        )

        # --------------------------------------------
        # POST WAKE
        # --------------------------------------------

        if (
            now - self.wake_time
            < POST_WAKE_IGNORE_SECONDS
        ):

            self.pre_roll.append(
                audio.copy()
            )

            return

        # --------------------------------------------
        # DIREZIONE
        # --------------------------------------------

        if self.locked_direction is None:

            # Fallback: la wake word non ha fornito una
            # direzione affidabile (es. livello troppo basso
            # nello storico), proviamo a costruirne una da zero
            # come meccanismo di sicurezza.
            self.update_direction_lock()

        direction_active = (
            self.direction_is_active()
        )

        # --------------------------------------------
        # PRE ROLL
        # --------------------------------------------

        self.pre_roll.append(
            audio.copy()
        )

        # --------------------------------------------
        # SPEECH START
        # --------------------------------------------

        energy_valid = (
            ratio >= SPEECH_START_RATIO
        )

        if (
            energy_valid
            and direction_active
        ):

            if self.speech_start_candidate is None:

                self.speech_start_candidate = now

                print(
                    f"[SPEECH] candidato "
                    f"RMS={rms:.0f} "
                    f"ratio={ratio:.2f} "
                    f"dir={self.locked_direction:.1f}°"
                )

            elif (
                now
                - self.speech_start_candidate
                >= SPEECH_START_TIME
            ):

                print(
                    "[SPEECH] Voce confermata"
                )

                self.command_audio = list(
                    self.pre_roll
                )

                self.command_audio.append(
                    audio.copy()
                )

                self.command_start_time = now

                self.last_speech_time = now

                self.state = self.RECORDING

                print(
                    f"[REC] Direction locked: "
                    f"{self.locked_direction:.1f}°"
                )

        else:

            self.speech_start_candidate = None

        # --------------------------------------------
        # TIMEOUT
        #
        # Non scatta se in questo momento c'è già un
        # candidato vocale attivo: significherebbe scartare
        # un'utterance reale appena rilevata (può succedere
        # se la ricostruzione della direzione, nel fallback,
        # ha richiesto quasi tutta la finestra di timeout).
        # Basta un ciclo in più per confermarlo o scartarlo
        # naturalmente.
        # --------------------------------------------

        if (
            self.speech_start_candidate is None
            and now - self.wake_time
            >= COMMAND_TIMEOUT_SECONDS
        ):

            print(
                "[WAIT] Timeout comando"
            )

            self.reset_to_listening()

    # ========================================================
    # RECORDING
    # ========================================================

    def process_recording(
        self,
        audio
    ):

        now = time.monotonic()

        mono = audio[:, 0]

        rms = self.speech_gate.rms(
            mono
        )

        ratio = self.speech_gate.ratio(
            rms
        )

        direction_active = (
            self.direction_is_active()
        )

        # ----------------------------------------------------
        # AUDIO REGISTRATO: SEMPRE TUTTO, NESSUN BUCO
        #
        # In ambienti riverberanti la direzione stimata può
        # saltare di decine di gradi sulla STESSA voce per via
        # degli echi. Zittire quei blocchi "fuori direzione"
        # tagliava pezzi veri di parlato a metà comando,
        # producendo audio a buchi per Vosk. Registriamo quindi
        # sempre il contenuto audio grezzo, senza filtrarlo.
        # ----------------------------------------------------

        filtered_audio = audio.copy()

        self.command_audio.append(
            filtered_audio
        )

        # ----------------------------------------------------
        # FINE COMANDO: la direzione conta di nuovo
        #
        # Per decidere se il comando è ancora in corso (e quindi
        # rimandare la chiusura) torniamo a richiedere che il
        # suono provenga dalla direzione bloccata (con la
        # consueta tolleranza/grazia). Senza questo, rumore o
        # eco continui da ALTRE direzioni impedirebbero mai al
        # rilevatore di silenzio di scattare, facendo durare
        # ogni comando fino al tetto massimo (MAX_COMMAND_SECONDS)
        # invece di chiudersi naturalmente sul vero silenzio
        # della sorgente che ci interessa.
        # ----------------------------------------------------

        speech_now = (
            direction_active
            and ratio >= SPEECH_END_RATIO
        )

        if speech_now:

            self.last_speech_time = now

        # ----------------------------------------------------
        # DEBUG (solo se DEBUG_LOGGING attivo)
        # ----------------------------------------------------

        if DEBUG_LOGGING:

            data = self.xvf.snapshot()

            direction = (
                data["dominant_angle"]
                if data["dominant_angle"] is not None
                else -1
            )

            silence_time = (
                now - self.last_speech_time
                if self.last_speech_time is not None
                else 0.0
            )

            direction_status = (
                "YES" if direction_active else "NO"
            )

            print(
                f"[REC] RMS={rms:6.0f} "
                f"ratio={ratio:4.2f} "
                f"dir={direction_status:3s} "
                f"DOM={direction:6.1f}° "
                f"silence={silence_time:4.2f}s"
            )

        # ----------------------------------------------------
        # FINE COMANDO
        # ----------------------------------------------------

        if (
            self.last_speech_time is not None
            and now - self.last_speech_time
            >= SPEECH_END_TIME
        ):

            print(
                "[REC] Fine comando"
            )

            self.finish_command()

            return

        # ----------------------------------------------------
        # MAX DURATA
        # ----------------------------------------------------

        if (
            now - self.command_start_time
            >= MAX_COMMAND_SECONDS
        ):

            print(
                "[REC] Durata massima comando"
            )

            self.finish_command()

    # ========================================================
    # SAVE WAV
    # ========================================================

    def finish_command(self):

        if not self.command_audio:

            print(
                "[REC] Nessun audio da salvare"
            )

            self.reset_to_listening()

            return

        # ----------------------------------------------------
        # PREPARAZIONE AUDIO (sempre eseguita)
        # ----------------------------------------------------

        try:

            audio = np.concatenate(
                self.command_audio,
                axis=0
            )

            # Limita la durata massima.
            max_samples = int(
                (
                    PRE_ROLL_SECONDS
                    + MAX_COMMAND_SECONDS
                    + SPEECH_END_TIME
                )
                * TARGET_SAMPLE_RATE
            )

            if len(audio) > max_samples:

                audio = audio[
                    -max_samples:
                ]

            # Teniamo SOLO il canale 0.
            mono = audio[:, 0]

            mono = np.asarray(
                mono,
                dtype=np.int16
            )

            duration = (
                len(mono)
                / TARGET_SAMPLE_RATE
            )

        except Exception as e:

            print(
                f"[REC] Errore preparazione audio: {e}"
            )

            self.reset_to_listening()

            return

        command_direction = (
            self.locked_direction
            if self.locked_direction is not None
            else -1.0
        )

        print()
        print(
            "================================================"
        )
        print(
            "[COMMAND] Comando catturato"
        )
        print(
            f"[COMMAND] Durata: {duration:.2f}s"
        )
        print(
            f"[COMMAND] Direzione: "
            f"{command_direction:.1f}°"
        )
        print(
            "================================================"
        )
        print()

        # ----------------------------------------------------
        # INOLTRO A VOSK (funzionale, prioritario: non deve
        # dipendere dal salvataggio WAV qui sotto)
        # ----------------------------------------------------

        if self.vosk_audio_queue is not None:

            try:

                self.vosk_audio_queue.put(
                    (
                        mono.tolist(),
                        TARGET_SAMPLE_RATE
                    )
                )

                print(
                    "[COMMAND] Audio inoltrato a Vosk"
                )

            except Exception as e:

                print(
                    f"[COMMAND] Errore inoltro a Vosk: {e}"
                )

        # ----------------------------------------------------
        # SALVATAGGIO WAV (solo debug/diagnostica, non critico)
        # ----------------------------------------------------

        try:

            with wave.open(
                COMMAND_WAV,
                "wb"
            ) as wf:

                wf.setnchannels(1)

                wf.setsampwidth(2)

                wf.setframerate(
                    TARGET_SAMPLE_RATE
                )

                wf.writeframes(
                    mono.tobytes()
                )

        except Exception as e:

            print(
                f"[REC] Errore salvataggio WAV: {e}"
            )

        self.reset_to_listening()

    # ========================================================
    # RESET
    # ========================================================

    def reset_to_listening(self):

        self.state = self.COOLDOWN

        self.cooldown_until = (
            time.monotonic()
            + 0.5
        )

        self.command_audio = []

        self.locked_direction = None

        self.direction_candidate = None

        self.direction_confirmations = 0

        self.direction_last_valid = 0.0

        self.speech_start_candidate = None

        self.last_speech_time = None

        self.pre_roll.clear()

    # ========================================================
    # MAIN LOOP
    # ========================================================

    def run(self):

        self.xvf.start()

        try:

            with sd.InputStream(
                device=self.device_index,
                samplerate=DEVICE_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=BLOCK_SIZE,
                callback=self._audio_callback,
                latency="low"
            ):

                print()
                print(
                    "================================================"
                )
                print(
                    " KeyVoice Wake Word Listener"
                )
                print(
                    " XVF3800 Directional Speech Gate"
                )
                print(
                    "================================================"
                )
                print(
                    f"Sample rate : "
                    f"{TARGET_SAMPLE_RATE}"
                )
                print(
                    f"Channels    : {CHANNELS}"
                )
                print(
                    f"Wake word   : {WAKEWORD}"
                )
                print(
                    f"Threshold   : "
                    f"high={WAKE_THRESHOLD_HIGH} "
                    f"low={WAKE_THRESHOLD_LOW} "
                    f"(x{WAKE_CONFIRM_CHUNKS} chunk)"
                )
                print(
                    f"Chunk/hop   : "
                    f"{WAKE_CHUNK_SAMPLES}/"
                    f"{WAKE_HOP_SAMPLES} campioni"
                )
                print(
                    f"Start ratio : "
                    f"{SPEECH_START_RATIO}"
                )
                print(
                    f"End ratio   : "
                    f"{SPEECH_END_RATIO}"
                )
                print(
                    f"Start time  : "
                    f"{SPEECH_START_TIME}s"
                )
                print(
                    f"End time    : "
                    f"{SPEECH_END_TIME}s"
                )
                print(
                    f"Direction   : "
                    f"{DIRECTION_ANGLE_TOLERANCE}°"
                )
                print(
                    f"Dominance   : "
                    f"{DIRECTION_MIN_DOMINANCE}x"
                )
                print(
                    f"Post wake   : "
                    f"{POST_WAKE_IGNORE_SECONDS}s"
                )
                print(
                    f"Pre-roll    : "
                    f"{PRE_ROLL_SECONDS}s"
                )
                print(
                    f"WAV output  : "
                    f"{COMMAND_WAV}"
                )
                print(
                    f"Beep file   : "
                    f"{BEEP_FILE}"
                )
                print(
                    "================================================"
                )
                print()

                self.calibrate_noise()

                print(
                    "[LISTENER] In ascolto..."
                )

                while (
                    self.stop_event is None
                    or not self.stop_event.is_set()
                ):

                    try:

                        audio = (
                            self.audio_queue.get(
                                timeout=1.0
                            )
                        )

                    except queue.Empty:

                        continue

                    now = time.monotonic()

                    # =================================================
                    # LISTENING
                    # =================================================

                    if self.state == self.LISTENING:

                        mono = audio[:, 0]

                        rms = (
                            self.speech_gate.rms(
                                mono
                            )
                        )

                        self.speech_gate.update(
                            rms
                        )

                        wake, score = (
                            self.check_wakeword(
                                audio
                            )
                        )

                        if (
                            now - self.last_alive_print
                            >= ALIVE_LOG_INTERVAL
                        ):

                            self.last_alive_print = now

                            print(
                                f"[LISTENER] in ascolto "
                                f"(score wake attuale: "
                                f"{self.last_wake_score:.3f})"
                            )

                        if wake:

                            print()
                            print(
                                f"[WAKE] Wake word "
                                f"rilevata "
                                f"score={score:.3f}"
                            )

                            self.play_beep()

                            self.wake_time = now

                            self.state = (
                                self.WAIT_COMMAND
                            )

                            self.pre_roll.clear()

                            self.command_audio = []

                            self.direction_candidate = None

                            self.direction_confirmations = 0

                            self.speech_start_candidate = None

                            self.wake_confirm_count = 0

                            # Catturiamo subito la direzione da
                            # cui è arrivata la wake word stessa:
                            # il comando verrà accettato solo se
                            # proviene dalla stessa sorgente,
                            # senza dover ricostruire un nuovo
                            # lock da zero (che tagliava l'inizio
                            # della frase).
                            wake_angle = (
                                self.xvf.recent_direction(
                                    WAKE_DIRECTION_LOOKBACK_SECONDS
                                )
                            )

                            self.locked_direction = wake_angle

                            if wake_angle is not None:

                                self.direction_last_valid = (
                                    time.monotonic()
                                )

                                print(
                                    f"[XVF] Direzione wake word: "
                                    f"{wake_angle:.1f}°"
                                )

                            else:

                                print(
                                    "[XVF] Direzione wake word "
                                    "non disponibile, la "
                                    "ricostruisco durante "
                                    "l'attesa del comando..."
                                )

                            print(
                                "[LISTENER] "
                                "Attendo comando..."
                            )

                    # =================================================
                    # WAIT COMMAND
                    # =================================================

                    elif (
                        self.state
                        == self.WAIT_COMMAND
                    ):

                        self.process_wait_command(
                            audio
                        )

                    # =================================================
                    # RECORDING
                    # =================================================

                    elif (
                        self.state
                        == self.RECORDING
                    ):

                        self.process_recording(
                            audio
                        )

                    # =================================================
                    # COOLDOWN
                    # =================================================

                    elif (
                        self.state
                        == self.COOLDOWN
                    ):

                        if (
                            now
                            >= self.cooldown_until
                        ):

                            self.state = (
                                self.LISTENING
                            )

                            self.pre_roll.clear()

                            print(
                                "[LISTENER] "
                                "Torno in ascolto..."
                            )

                if (
                    self.stop_event is not None
                    and self.stop_event.is_set()
                ):

                    print(
                        "[LISTENER] "
                        "Stop richiesto, chiusura..."
                    )

        except KeyboardInterrupt:

            print()
            print(
                "[LISTENER] Interrotto"
            )

        finally:

            self.xvf.stop()


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    listener = WakeWordListener()

    listener.run()