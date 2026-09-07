import os
import sys
import time
import threading
import queue
import subprocess
import collections
import wave
import math

import numpy as np
import sounddevice as sd
from openwakeword.model import Model


# ============================================================
# CONFIGURAZIONE
# ============================================================

TARGET_SAMPLE_RATE = 16000
DEVICE_SAMPLE_RATE = 16000
CHANNELS = 2

BLOCK_MS = 30
BLOCK_SIZE = int(DEVICE_SAMPLE_RATE * BLOCK_MS / 1000)

WAKEWORD = "hey_jarvis"
WAKE_THRESHOLD = 0.35

# Stampa lo score della wake word anche quando resta sotto
# soglia, per poter tarare WAKE_THRESHOLD osservando i valori
# reali durante l'uso.
WAKE_DEBUG_INTERVAL = 1.0

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

DIRECTION_ANGLE_TOLERANCE = 35.0

DIRECTION_LOST_GRACE_SECONDS = 0.18

# Quando scatta la wake word, cerchiamo nello storico telemetria
# la lettura di direzione più energica negli ultimi N secondi:
# copre la coda dell'utterance "hey jarvis" (l'engine di wake
# word finalizza il riconoscimento con un piccolo ritardo dopo
# che la frase è stata pronunciata).
WAKE_DIRECTION_LOOKBACK_SECONDS = 0.6

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

BEEP_FILE = "/home/homeassistant/KeyVoice/sounds/wake.wav"
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
                # DEBUG TELEMETRIA
                # ------------------------------------------------

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

    def __init__(self):

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

                return index

        raise RuntimeError(
            "Dispositivo XVF3800 non trovato"
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

        try:

            subprocess.Popen(
                [
                    "aplay",
                    "-q",
                    "-D",
                    BEEP_DEVICE,
                    BEEP_FILE
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )

        except Exception as e:

            print(
                f"[BEEP] Errore: {e}"
            )

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

        mono = audio[:, 0]

        prediction = self.model.predict(
            mono
        )

        score = prediction.get(
            WAKEWORD,
            0.0
        )

        now = time.monotonic()

        if (
            now - self.last_wake_debug
            >= WAKE_DEBUG_INTERVAL
        ):

            self.last_wake_debug = now

            print(
                f"[WAKE-DEBUG] score={score:.3f} "
                f"threshold={WAKE_THRESHOLD}"
            )

        if score >= WAKE_THRESHOLD:

            return True, score

        return False, score

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
        # --------------------------------------------

        if (
            now - self.wake_time
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
        # FILTRO DIREZIONALE
        #
        # Se la direzione è quella della voce:
        # manteniamo il campione.
        #
        # Se la direzione cambia:
        # sostituiamo il campione con silenzio.
        # ----------------------------------------------------

        if direction_active:

            filtered_audio = audio.copy()

        else:

            filtered_audio = np.zeros_like(
                audio
            )

        self.command_audio.append(
            filtered_audio
        )

        directional_speech = (
            direction_active
            and ratio >= SPEECH_END_RATIO
        )

        if directional_speech:

            self.last_speech_time = now

        # ----------------------------------------------------
        # DEBUG
        # ----------------------------------------------------

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

        try:

            audio = np.concatenate(
                self.command_audio,
                axis=0
            )

            # ------------------------------------------------
            # Limita la durata massima.
            # ------------------------------------------------

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

            # ------------------------------------------------
            # Salva SOLO il canale 0.
            # ------------------------------------------------

            mono = audio[:, 0]

            mono = np.asarray(
                mono,
                dtype=np.int16
            )

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

            duration = (
                len(mono)
                / TARGET_SAMPLE_RATE
            )

            print()
            print(
                "================================================"
            )
            print(
                "[COMMAND] WAV pronto"
            )
            print(
                f"[COMMAND] File: {COMMAND_WAV}"
            )
            print(
                f"[COMMAND] Durata: {duration:.2f}s"
            )
            command_direction = (
                self.locked_direction
                if self.locked_direction is not None
                else -1.0
            )

            print(
                f"[COMMAND] Direzione: "
                f"{command_direction:.1f}°"
            )
            print(
                "================================================"
            )
            print()

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
                    f"{WAKE_THRESHOLD}"
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
                    "================================================"
                )
                print()

                self.calibrate_noise()

                print(
                    "[LISTENER] In ascolto..."
                )

                while True:

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