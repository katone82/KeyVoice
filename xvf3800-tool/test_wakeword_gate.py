#!/usr/bin/env python3

import time
import queue
import subprocess
import collections
import wave

import numpy as np
import sounddevice as sd
from openwakeword.model import Model


# ============================================================
# AUDIO
# ============================================================

TARGET_SAMPLE_RATE = 16000
DEVICE_SAMPLE_RATE = 16000

CHANNELS = 2

INPUT_DEVICE_NAME = "reSpeaker XVF3800 4-Mic Array"

BLOCK_MS = 30
BLOCK_SIZE = int(
    TARGET_SAMPLE_RATE * BLOCK_MS / 1000
)


# ============================================================
# WAKE WORD
# ============================================================

WAKEWORD_MODEL = "hey_jarvis"

WAKEWORD_THRESHOLD = 0.35


# ============================================================
# SPEECH GATE
# ============================================================

# Energia minima relativa al rumore per iniziare
# un candidato vocale.
SPEECH_START_RATIO = 2.5

# Energia minima relativa al rumore per considerare
# ancora attiva la voce durante la registrazione.
SPEECH_END_RATIO = 1.5


# IMPORTANTE:
#
# Prima avevamo 0.12s.
#
# 0.12s = circa 4 blocchi da 30ms.
#
# Per evitare picchi/transienti isolati utilizziamo
# una conferma più lunga.
#
SPEECH_START_TIME = 0.24


# Silenzio necessario per terminare il comando.
SPEECH_END_TIME = 0.50


# ============================================================
# PRE-ROLL
# ============================================================

PRE_ROLL_SECONDS = 0.30


# ============================================================
# NOISE FLOOR
# ============================================================

# Durata calibrazione iniziale.
NOISE_LEARN_SECONDS = 2.0


# Finestra usata per seguire lentamente il rumore ambientale.
NOISE_WINDOW_SECONDS = 2.0


# Un frame viene considerato "rumore" soltanto se
# non supera questa proporzione rispetto al noise floor.
#
# Se:
#
#   RMS > noise * 1.5
#
# non lo usiamo per aggiornare il noise floor.
NOISE_MAX_RATIO = 1.5


# Aggiornamento molto lento.
#
# Serve a evitare che un rumore improvviso o una voce
# facciano salire immediatamente la soglia.
NOISE_UPDATE_ALPHA = 0.02


# ============================================================
# POST WAKE WORD
# ============================================================

# Dopo la wake word ignoriamo completamente l'audio
# per questo intervallo.
#
# Serve per:
#
# - beep
# - coda della wake word
# - riverbero
# - transienti
#
POST_WAKE_IGNORE_SECONDS = 0.40


# ============================================================
# COMMAND WAIT
# ============================================================

# Tempo massimo per iniziare a parlare dopo la wake word.
WAIT_COMMAND_TIMEOUT = 4.0


# ============================================================
# COMMAND
# ============================================================

MAX_COMMAND_SECONDS = 6.0


# ============================================================
# COOLDOWN
# ============================================================

COOLDOWN_SECONDS = 0.80


# ============================================================
# WATCHDOG
# ============================================================

AUDIO_WATCHDOG_SECONDS = 3.0


# ============================================================
# BEEP
# ============================================================

BEEP_FILE = (
    "/home/homeassistant/KeyVoice/sounds/wake.wav"
)

BEEP_DEVICE = "plughw:4,0"


# ============================================================
# STATES
# ============================================================

LISTENING = "LISTENING"
WAIT_COMMAND = "WAIT_COMMAND"
RECORDING = "RECORDING"
COOLDOWN = "COOLDOWN"


# ============================================================
# ADAPTIVE SPEECH GATE
# ============================================================

class AdaptiveSpeechGate:

    def __init__(self):

        self.noise_floor = None

        self.samples = collections.deque(
            maxlen=max(
                1,
                int(
                    NOISE_WINDOW_SECONDS
                    / (BLOCK_MS / 1000)
                )
            )
        )

        self.calibration_samples = []

        self.calibrating = True

        self.calibration_start = time.monotonic()


    # --------------------------------------------------------
    # RMS
    # --------------------------------------------------------

    @staticmethod
    def rms(audio):

        if len(audio) == 0:
            return 0.0

        x = audio.astype(np.float32)

        return float(
            np.sqrt(
                np.mean(x * x) + 1e-12
            )
        )


    # --------------------------------------------------------
    # CALIBRATION
    # --------------------------------------------------------

    def calibration_update(self, rms):

        if rms <= 0:
            return False

        self.calibration_samples.append(rms)

        elapsed = (
            time.monotonic()
            - self.calibration_start
        )

        if elapsed < NOISE_LEARN_SECONDS:
            return False

        if self.calibration_samples:

            # La mediana evita che un breve rumore
            # durante la calibrazione domini il risultato.
            self.noise_floor = float(
                np.median(
                    self.calibration_samples
                )
            )

        else:

            self.noise_floor = rms

        self.samples.clear()

        self.calibrating = False

        print()
        print(
            "[CAL] "
            f"Noise floor iniziale: "
            f"{self.noise_floor:.1f}"
        )

        print(
            "[LISTENER] "
            "In ascolto..."
        )

        return True


    # --------------------------------------------------------
    # UPDATE NOISE
    # --------------------------------------------------------

    def update_noise(self, rms):

        if rms <= 0:
            return

        if self.noise_floor is None:

            self.noise_floor = rms

            return

        ratio = (
            rms / self.noise_floor
        )


        # ----------------------------------------------------
        # PROTEZIONE CONTRO VOCE / TRANSIENTI
        # ----------------------------------------------------
        #
        # Se il segnale è molto superiore al rumore,
        # NON deve diventare parte del rumore.
        #

        if ratio > NOISE_MAX_RATIO:

            return


        self.samples.append(rms)

        if not self.samples:
            return


        # Noise stimato usando la mediana della finestra.
        median_noise = float(
            np.median(
                self.samples
            )
        )


        # Adattamento lento.
        self.noise_floor = (
            (
                1.0
                - NOISE_UPDATE_ALPHA
            )
            * self.noise_floor
            +
            NOISE_UPDATE_ALPHA
            * median_noise
        )


    # --------------------------------------------------------
    # RATIO
    # --------------------------------------------------------

    def ratio(self, rms):

        if (
            self.noise_floor is None
            or self.noise_floor <= 0
        ):
            return 0.0

        return (
            rms / self.noise_floor
        )


    # --------------------------------------------------------
    # SPEECH TEST
    # --------------------------------------------------------

    def is_speech(
        self,
        rms,
        threshold
    ):

        return (
            self.ratio(rms)
            >= threshold
        )


# ============================================================
# AUDIO WATCHDOG
# ============================================================

class AudioWatchdog:

    def __init__(self):

        self.last_audio = (
            time.monotonic()
        )


    def update(self):

        self.last_audio = (
            time.monotonic()
        )


    def check(self):

        return (
            time.monotonic()
            - self.last_audio
            <= AUDIO_WATCHDOG_SECONDS
        )


# ============================================================
# LISTENER
# ============================================================

class WakeWordListener:

    def __init__(self):

        self.state = LISTENING

        self.audio_queue = queue.Queue(
            maxsize=100
        )

        self.command_audio = []

        self.state_start = (
            time.monotonic()
        )

        self.watchdog = AudioWatchdog()

        self.speech_gate = (
            AdaptiveSpeechGate()
        )


        # ----------------------------------------------------
        # Speech candidate
        # ----------------------------------------------------

        self.speech_candidate_start = None

        self.speech_candidate_audio = []


        # ----------------------------------------------------
        # Recording
        # ----------------------------------------------------

        self.speech_start_time = None

        self.last_speech_time = None


        # ----------------------------------------------------
        # Wake
        # ----------------------------------------------------

        self.post_wake_ignore_until = 0


        # ----------------------------------------------------
        # Cooldown
        # ----------------------------------------------------

        self.cooldown_start = None


        # ----------------------------------------------------
        # OpenWakeWord
        # ----------------------------------------------------

        print()
        print(
            "[INIT] Loading OpenWakeWord..."
        )

        self.oww = Model(
            wakeword_models=[
                WAKEWORD_MODEL
            ]
        )

        print(
            "[INIT] OpenWakeWord loaded"
        )


        # ----------------------------------------------------
        # Audio device
        # ----------------------------------------------------

        self.device = (
            self.find_input_device()
        )

        print(
            f"[INIT] Input device: "
            f"{self.device}"
        )


    # ========================================================
    # FIND INPUT DEVICE
    # ========================================================

    def find_input_device(self):

        devices = sd.query_devices()

        for index, device in enumerate(devices):

            name = device["name"]

            if (
                INPUT_DEVICE_NAME.lower()
                in name.lower()
            ):

                print(
                    f"[AUDIO] "
                    f"Found device {index}: "
                    f"{name}"
                )

                return index

        raise RuntimeError(
            "Input device not found: "
            f"{INPUT_DEVICE_NAME}"
        )


    # ========================================================
    # AUDIO CALLBACK
    # ========================================================

    def audio_callback(
        self,
        indata,
        frames,
        callback_time,
        status
    ):

        if status:

            print(
                f"[AUDIO] {status}"
            )

        self.watchdog.update()

        try:

            audio = indata.copy()

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
                f"[BEEP] Error: {e}"
            )


    # ========================================================
    # WAKE WORD
    # ========================================================

    def detect_wakeword(self, audio):

        if len(audio) == 0:
            return False

        # Utilizziamo il canale 0.
        channel = audio[:, 0]

        pcm = np.asarray(
            channel,
            dtype=np.float32
        )

        try:

            prediction = (
                self.oww.predict(pcm)
            )

        except Exception as e:

            print(
                f"[OWW] Error: {e}"
            )

            return False

        score = prediction.get(
            WAKEWORD_MODEL,
            0.0
        )

        if score >= WAKEWORD_THRESHOLD:

            print()
            print(
                "[WAKE] "
                f"{WAKEWORD_MODEL} "
                f"score={score:.3f}"
            )

            return True

        return False


    # ========================================================
    # STATE
    # ========================================================

    def set_state(self, state):

        if state != self.state:

            print(
                f"[STATE] "
                f"{self.state} -> {state}"
            )

        self.state = state

        self.state_start = (
            time.monotonic()
        )


    # ========================================================
    # RESET COMMAND
    # ========================================================

    def reset_command(self):

        self.command_audio.clear()

        self.speech_candidate_audio.clear()

        self.speech_candidate_start = None

        self.speech_start_time = None

        self.last_speech_time = None


    # ========================================================
    # LISTENING
    # ========================================================

    def process_listening(self, audio):

        channel = audio[:, 0]

        rms = self.speech_gate.rms(
            channel
        )


        # ----------------------------------------------------
        # CALIBRATION
        # ----------------------------------------------------

        if self.speech_gate.calibrating:

            elapsed = (
                time.monotonic()
                - self.speech_gate.calibration_start
            )

            self.speech_gate.calibration_update(
                rms
            )

            if self.speech_gate.calibrating:

                print(
                    "[CAL] "
                    f"RMS={rms:.1f} "
                    f"elapsed={elapsed:.1f}s"
                )

            return


        # ----------------------------------------------------
        # WAKE WORD
        # ----------------------------------------------------
        #
        # La wake word viene controllata PRIMA
        # dell'aggiornamento del noise floor.
        #
        # Quindi il picco della wake word non viene
        # utilizzato per aumentare il noise floor.
        #

        if self.detect_wakeword(audio):

            self.reset_command()


            # ------------------------------------------------
            # POST WAKE BLOCK
            # ------------------------------------------------

            self.post_wake_ignore_until = (
                time.monotonic()
                + POST_WAKE_IGNORE_SECONDS
            )

            print(
                "[GATE] "
                "Ignore post-wake: "
                f"{POST_WAKE_IGNORE_SECONDS:.2f}s"
            )


            # ------------------------------------------------
            # BEEP
            # ------------------------------------------------

            self.play_beep()


            self.set_state(
                WAIT_COMMAND
            )

            return


        # ----------------------------------------------------
        # NOISE UPDATE
        # ----------------------------------------------------
        #
        # Avviene SOLO mentre siamo in LISTENING.
        #
        # Durante WAIT_COMMAND e RECORDING il noise floor
        # resta congelato.
        #

        self.speech_gate.update_noise(
            rms
        )


    # ========================================================
    # WAIT COMMAND
    # ========================================================

    def process_wait_command(self, audio):

        channel = audio[:, 0]

        rms = self.speech_gate.rms(
            channel
        )

        ratio = self.speech_gate.ratio(
            rms
        )

        now = time.monotonic()


        # ----------------------------------------------------
        # POST WAKE IGNORE
        # ----------------------------------------------------

        if (
            now
            < self.post_wake_ignore_until
        ):

            remaining = (
                self.post_wake_ignore_until
                - now
            )

            print(
                "[WAIT] "
                f"post-wake ignore "
                f"{remaining:.2f}s"
            )

            return


        # ----------------------------------------------------
        # NOISE
        # ----------------------------------------------------

        noise = (
            self.speech_gate.noise_floor
            if self.speech_gate.noise_floor
            is not None
            else 0.0
        )


        print(
            "[WAIT] "
            f"RMS={rms:.1f} "
            f"noise={noise:.1f} "
            f"ratio={ratio:.2f}"
        )


        # ----------------------------------------------------
        # ABOVE START THRESHOLD
        # ----------------------------------------------------

        if self.speech_gate.is_speech(
            rms,
            SPEECH_START_RATIO
        ):


            # ------------------------------------------------
            # START CANDIDATE
            # ------------------------------------------------

            if (
                self.speech_candidate_start
                is None
            ):

                self.speech_candidate_start = now

                self.speech_candidate_audio = []

                print(
                    "[GATE] "
                    "Speech candidate started"
                )


            # ------------------------------------------------
            # CANDIDATE AUDIO
            # ------------------------------------------------

            self.speech_candidate_audio.append(
                audio.copy()
            )


            # ------------------------------------------------
            # CANDIDATE DURATION
            # ------------------------------------------------

            candidate_time = (
                now
                - self.speech_candidate_start
            )


            print(
                "[GATE] "
                f"candidate={candidate_time:.2f}s"
            )


            # ------------------------------------------------
            # CONFIRM SPEECH
            # ------------------------------------------------

            if (
                candidate_time
                >= SPEECH_START_TIME
            ):

                print(
                    "[SPEECH] "
                    f"Speech confirmed "
                    f"({candidate_time:.2f}s)"
                )


                # ------------------------------------------------
                # COMMAND AUDIO
                # ------------------------------------------------
                #
                # Conserviamo il candidato come inizio
                # del comando.
                #

                self.command_audio = (
                    list(
                        self.speech_candidate_audio
                    )
                )


                # ------------------------------------------------
                # RECORDING
                # ------------------------------------------------

                self.speech_start_time = now

                self.last_speech_time = now

                self.speech_candidate_start = None

                self.speech_candidate_audio.clear()

                self.set_state(
                    RECORDING
                )

                return


        else:

            # ------------------------------------------------
            # PICCO ISOLATO
            # ------------------------------------------------
            #
            # Se il segnale torna sotto soglia prima della
            # conferma, il candidato viene completamente
            # scartato.
            #

            if (
                self.speech_candidate_start
                is not None
            ):

                candidate_time = (
                    now
                    - self.speech_candidate_start
                )

                print(
                    "[GATE] "
                    f"Candidate rejected "
                    f"after {candidate_time:.2f}s"
                )


            self.speech_candidate_start = None

            self.speech_candidate_audio.clear()


        # ----------------------------------------------------
        # WAIT TIMEOUT
        # ----------------------------------------------------

        if (
            now
            - self.state_start
            >= WAIT_COMMAND_TIMEOUT
        ):

            print()

            print(
                "[WAIT] "
                "Command timeout"
            )

            self.reset_command()

            self.set_state(
                LISTENING
            )


    # ========================================================
    # RECORDING
    # ========================================================

    def process_recording(self, audio):

        channel = audio[:, 0]

        rms = self.speech_gate.rms(
            channel
        )

        ratio = self.speech_gate.ratio(
            rms
        )

        now = time.monotonic()


        # ----------------------------------------------------
        # APPEND AUDIO
        # ----------------------------------------------------

        self.command_audio.append(
            audio.copy()
        )


        # ----------------------------------------------------
        # SPEECH ACTIVE
        # ----------------------------------------------------

        if (
            ratio
            >= SPEECH_END_RATIO
        ):

            self.last_speech_time = now


        silence_time = (
            now
            - self.last_speech_time
        )


        print(
            "[REC] "
            f"RMS={rms:.1f} "
            f"ratio={ratio:.2f} "
            f"silence={silence_time:.2f}s"
        )


        # ----------------------------------------------------
        # END OF COMMAND
        # ----------------------------------------------------

        if (
            silence_time
            >= SPEECH_END_TIME
        ):

            print(
                "[SPEECH] "
                f"End detected "
                f"(silence={silence_time:.2f}s)"
            )

            self.finish_command()

            return


        # ----------------------------------------------------
        # MAX COMMAND
        # ----------------------------------------------------

        command_time = (
            now
            - self.speech_start_time
        )

        if (
            command_time
            >= MAX_COMMAND_SECONDS
        ):

            print(
                "[RECORD] "
                "Maximum command time"
            )

            self.finish_command()


    # ========================================================
    # FINISH COMMAND
    # ========================================================

    def finish_command(self):

        if not self.command_audio:

            self.set_state(
                LISTENING
            )

            return


        # ----------------------------------------------------
        # CONCAT
        # ----------------------------------------------------

        audio = np.concatenate(
            self.command_audio,
            axis=0
        )


        # ----------------------------------------------------
        # LIMIT AUDIO
        # ----------------------------------------------------

        max_samples = int(
            (
                PRE_ROLL_SECONDS
                + MAX_COMMAND_SECONDS
            )
            * TARGET_SAMPLE_RATE
        )


        if len(audio) > max_samples:

            audio = audio[-max_samples:]


        # ----------------------------------------------------
        # DURATION
        # ----------------------------------------------------

        duration = (
            len(audio)
            / TARGET_SAMPLE_RATE
        )


        print()
        print(
            "[RECORD] "
            f"Command audio: "
            f"{duration:.2f}s"
        )


        # ----------------------------------------------------
        # SAVE WAV
        # ----------------------------------------------------

        filename = (
            "/tmp/keyvoice_command.wav"
        )


        try:

            mono = np.asarray(
                audio[:, 0],
                dtype=np.int16
            )


            with wave.open(
                filename,
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


            print(
                "[RECORD] "
                f"Saved: {filename}"
            )


        except Exception as e:

            print(
                "[RECORD] "
                f"Save error: {e}"
            )


        print(
            "[RECORD] "
            "Ready for Vosk processing"
        )


        # ----------------------------------------------------
        # COOLDOWN
        # ----------------------------------------------------

        self.set_state(
            COOLDOWN
        )

        self.cooldown_start = (
            time.monotonic()
        )


    # ========================================================
    # COOLDOWN
    # ========================================================

    def process_cooldown(self, audio):

        if (
            time.monotonic()
            - self.cooldown_start
            >= COOLDOWN_SECONDS
        ):

            self.reset_command()

            self.set_state(
                LISTENING
            )


    # ========================================================
    # PROCESS AUDIO
    # ========================================================

    def process_audio(self, audio):

        if self.state == LISTENING:

            self.process_listening(
                audio
            )

        elif self.state == WAIT_COMMAND:

            self.process_wait_command(
                audio
            )

        elif self.state == RECORDING:

            self.process_recording(
                audio
            )

        elif self.state == COOLDOWN:

            self.process_cooldown(
                audio
            )


    # ========================================================
    # RUN
    # ========================================================

    def run(self):

        print()
        print(
            "================================================"
        )
        print(
            " KeyVoice Wake Word Listener"
        )
        print(
            "================================================"
        )

        print(
            f"Sample rate : "
            f"{TARGET_SAMPLE_RATE}"
        )

        print(
            f"Channels    : "
            f"{CHANNELS}"
        )

        print(
            f"Wake word   : "
            f"{WAKEWORD_MODEL}"
        )

        print(
            f"Threshold   : "
            f"{WAKEWORD_THRESHOLD}"
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
            f"Noise cal   : "
            f"{NOISE_LEARN_SECONDS}s"
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
            "================================================"
        )

        print()


        with sd.InputStream(

            device=self.device,

            samplerate=DEVICE_SAMPLE_RATE,

            channels=CHANNELS,

            dtype="int16",

            blocksize=BLOCK_SIZE,

            callback=self.audio_callback,

            latency="low"

        ):

            print(
                "[LISTENER] "
                "Calibrazione rumore..."
            )


            while True:

                try:

                    audio = (
                        self.audio_queue.get(
                            timeout=1.0
                        )
                    )


                except queue.Empty:

                    if not self.watchdog.check():

                        print(
                            "[WATCHDOG] "
                            "Audio stream timeout"
                        )

                    continue


                self.process_audio(
                    audio
                )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    try:

        listener = (
            WakeWordListener()
        )

        listener.run()


    except KeyboardInterrupt:

        print()

        print(
            "[EXIT] "
            "Listener stopped"
        )


    except Exception as e:

        print()

        print(
            f"[FATAL] {e}"
        )

        raise