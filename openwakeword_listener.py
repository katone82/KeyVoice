import os
import subprocess
import threading
import time
import wave
from typing import List

import numpy as np
import pyaudio
import webrtcvad
from scipy.signal import resample_poly

from openwakeword.model import Model


# ============================================================
# AUDIO CONFIGURATION
# ============================================================

TARGET_SAMPLE_RATE = 16_000
DEVICE_SAMPLE_RATE = 48_000

INPUT_DEVICE_INDEX = 0

CHANNELS = 1
AUDIO_FORMAT = pyaudio.paInt16

# openWakeWord:
# 80 ms a 16 kHz = 1280 samples
OWW_FRAME_LENGTH = 1_280

# Microfono:
# 80 ms a 48 kHz = 3840 samples
DEVICE_FRAME_LENGTH = OWW_FRAME_LENGTH * 3


# ============================================================
# LISTENER STATES
# ============================================================

STATE_LISTENING = "listening"
STATE_WAIT_COMMAND = "wait_command"
STATE_RECORDING = "recording"
STATE_COOLDOWN = "cooldown"


# ============================================================
# BEEP
# ============================================================

BEEP_FILE = "/home/homeassistant/KeyVoice/sounds/wake.wav"
BEEP_DEVICE = "plughw:3,0"


def play_beep() -> None:
    """
    Riproduce il beep quando viene riconosciuta la wake word.
    """

    if not os.path.exists(BEEP_FILE):
        print(
            f"[BEEP] File non trovato: {BEEP_FILE}"
        )
        return

    try:
        subprocess.run(
            [
                "aplay",
                "-q",
                "-D",
                BEEP_DEVICE,
                BEEP_FILE
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2
        )

    except subprocess.TimeoutExpired:
        print(
            "[BEEP] Timeout riproduzione"
        )

    except Exception as exc:
        print(
            f"[BEEP] Errore: {exc}"
        )


# ============================================================
# AUDIO CONVERSION
# ============================================================

def convert_48k_to_16k(
    pcm_bytes: bytes
) -> np.ndarray:
    """
    Converte PCM mono int16:

        48 kHz -> 16 kHz
    """

    audio_48k = np.frombuffer(
        pcm_bytes,
        dtype=np.int16
    )

    audio_16k = resample_poly(
        audio_48k.astype(np.float32),
        up=1,
        down=3
    )

    return np.clip(
        audio_16k,
        -32768,
        32767
    ).astype(np.int16)


# ============================================================
# AUDIO READ
# ============================================================

def read_audio_chunk(
    stream: pyaudio.Stream
) -> np.ndarray:
    """
    Legge circa 80 ms dal microfono a 48 kHz
    e restituisce audio a 16 kHz.
    """

    pcm = stream.read(
        DEVICE_FRAME_LENGTH,
        exception_on_overflow=False
    )

    return convert_48k_to_16k(
        pcm
    )


# ============================================================
# DEBUG AUDIO
# ============================================================

def save_debug_audio(
    buffer: List[int],
    sample_rate: int
) -> None:
    """
    Salva il comando inviato a Vosk.
    """

    debug_dir = "debug_audio"

    os.makedirs(
        debug_dir,
        exist_ok=True
    )

    timestamp = time.strftime(
        "%Y%m%d_%H%M%S"
    )

    filename = os.path.join(
        debug_dir,
        f"command_{timestamp}.wav"
    )

    audio_data = np.asarray(
        buffer,
        dtype=np.int16
    )

    with wave.open(
        filename,
        "wb"
    ) as wav_file:

        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(
            sample_rate
        )

        wav_file.writeframes(
            audio_data.tobytes()
        )

    print(
        f"[DEBUG] Audio salvato: {filename}"
    )


# ============================================================
# OPENWAKEWORD LISTENER
# ============================================================

def openwakeword_listener(
    audio_queue,
    stop_event: threading.Event,
    config: dict
) -> None:

    # ========================================================
    # CONFIG
    # ========================================================

    model_name = config.get(
        "model",
        "hey_jarvis"
    )

    threshold = config.get(
        "threshold",
        0.35
    )

    save_debug = config.get(
        "save_debug_audio",
        False
    )

    vad_mode = config.get(
        "vad_mode",
        2
    )

    vad_voice_start_timeout = config.get(
        "vad_voice_start_timeout",
        1.5
    )

    vad_voice_end_sec = config.get(
        "vad_voice_end_sec",
        0.6
    )

    max_command_seconds = config.get(
        "max_command_seconds",
        6.0
    )

    min_command_seconds = config.get(
        "min_command_seconds",
        0.4
    )

    post_buffer_seconds = config.get(
        "post_buffer_seconds",
        0.1
    )

    wakeword_cooldown_sec = config.get(
        "wakeword_cooldown_sec",
        2.0
    )

    # Ogni quanti secondi rigenerare:
    # - stream PyAudio
    # - modello openWakeWord
    #
    # Durante i test consiglio 300 = 5 minuti.
    listener_refresh_seconds = config.get(
        "listener_refresh_seconds",
        300
    )

    print(
        "[OPENWAKEWORD] Thread partito"
    )

    print(
        f"[OPENWAKEWORD] Modello: {model_name}"
    )

    print(
        f"[OPENWAKEWORD] Threshold: {threshold}"
    )

    print(
        f"[OPENWAKEWORD] VAD mode: {vad_mode}"
    )

    print(
        "[OPENWAKEWORD] "
        f"Timeout inizio comando: "
        f"{vad_voice_start_timeout}s"
    )

    print(
        "[OPENWAKEWORD] "
        f"Silenzio fine comando: "
        f"{vad_voice_end_sec}s"
    )

    print(
        "[OPENWAKEWORD] "
        f"Durata massima comando: "
        f"{max_command_seconds}s"
    )

    print(
        "[OPENWAKEWORD] "
        f"Cooldown: "
        f"{wakeword_cooldown_sec}s"
    )

    print(
        "[OPENWAKEWORD] "
        f"Refresh listener: "
        f"{listener_refresh_seconds}s"
    )

    # ========================================================
    # PYAUDIO
    # ========================================================

    audio = pyaudio.PyAudio()

    stream = None
    model = None

    model_created_at = 0.0

    # ========================================================
    # MODEL CREATION
    # ========================================================

    def create_wake_model():
        """
        Ricrea completamente openWakeWord.
        """

        print(
            "[OPENWAKEWORD] "
            "Caricamento modello..."
        )

        new_model = Model(
            wakeword_models=[
                model_name
            ],
            inference_framework="onnx"
        )

        print(
            "[OPENWAKEWORD] "
            "Modello caricato"
        )

        return new_model

    # ========================================================
    # MICROPHONE OPEN
    # ========================================================

    def open_microphone():
        """
        Apre lo stream del microfono.
        """

        device_info = (
            audio.get_device_info_by_index(
                INPUT_DEVICE_INDEX
            )
        )

        print(
            "[OPENWAKEWORD] "
            f"Microfono: "
            f"{device_info['name']}"
        )

        print(
            "[OPENWAKEWORD] "
            f"Device index: "
            f"{INPUT_DEVICE_INDEX}"
        )

        print(
            "[OPENWAKEWORD] "
            f"Capture audio: "
            f"{DEVICE_SAMPLE_RATE} Hz"
        )

        print(
            "[OPENWAKEWORD] "
            f"Processing audio: "
            f"{TARGET_SAMPLE_RATE} Hz"
        )

        new_stream = audio.open(
            rate=DEVICE_SAMPLE_RATE,
            channels=CHANNELS,
            format=AUDIO_FORMAT,
            input=True,
            input_device_index=INPUT_DEVICE_INDEX,
            frames_per_buffer=DEVICE_FRAME_LENGTH
        )

        print(
            "[OPENWAKEWORD] "
            "Microfono aperto"
        )

        return new_stream

    # ========================================================
    # MICROPHONE CLOSE
    # ========================================================

    def close_microphone(
        current_stream
    ) -> None:

        if current_stream is None:
            return

        try:

            if current_stream.is_active():
                current_stream.stop_stream()

        except Exception:
            pass

        try:

            current_stream.close()

        except Exception:
            pass

    # ========================================================
    # INITIAL MODEL + MICROPHONE
    # ========================================================

    try:

        model = create_wake_model()

        model_created_at = (
            time.monotonic()
        )

        stream = open_microphone()

    except Exception as exc:

        print(
            "[OPENWAKEWORD] "
            f"Errore inizializzazione: {exc}"
        )

        close_microphone(
            stream
        )

        audio.terminate()

        stop_event.set()

        return

    print(
        "[OPENWAKEWORD] "
        "Listener pronto"
    )

    # ========================================================
    # VAD
    # ========================================================

    vad = webrtcvad.Vad()

    vad.set_mode(
        vad_mode
    )

    vad_frame_ms = 30

    vad_frame_length = int(
        TARGET_SAMPLE_RATE
        * vad_frame_ms
        / 1000
    )

    # ========================================================
    # BUFFERS
    # ========================================================

    audio_buffer: List[int] = []
    vad_buffer: List[int] = []

    # ========================================================
    # STATE
    # ========================================================

    state = STATE_LISTENING

    wait_command_start = None
    command_start_time = None
    last_voice_time = None

    cooldown_until = None

    refresh_pending = False

    # ========================================================
    # BUFFER RESET
    # ========================================================

    def clear_buffers() -> None:

        audio_buffer.clear()
        vad_buffer.clear()

    # ========================================================
    # ENTER LISTENING
    # ========================================================

    def enter_listening(
        reason: str = ""
    ) -> None:

        nonlocal state
        nonlocal wait_command_start
        nonlocal command_start_time
        nonlocal last_voice_time
        nonlocal cooldown_until

        clear_buffers()

        wait_command_start = None
        command_start_time = None
        last_voice_time = None
        cooldown_until = None

        state = STATE_LISTENING

        if reason:

            print(
                "[LISTENER] "
                f"In ascolto wake word "
                f"({reason})"
            )

        else:

            print(
                "[LISTENER] "
                "In ascolto wake word"
            )

    # ========================================================
    # ENTER COOLDOWN
    # ========================================================

    def enter_cooldown(
        reason: str
    ) -> None:

        nonlocal state
        nonlocal wait_command_start
        nonlocal command_start_time
        nonlocal last_voice_time
        nonlocal cooldown_until

        clear_buffers()

        wait_command_start = None
        command_start_time = None
        last_voice_time = None

        cooldown_until = (
            time.monotonic()
            + wakeword_cooldown_sec
        )

        state = STATE_COOLDOWN

        print(
            "[LISTENER] "
            f"Cooldown ({reason})"
        )

    # ========================================================
    # VAD
    # ========================================================

    def process_vad_frames() -> bool:

        speech_detected = False

        while (
            len(vad_buffer)
            >= vad_frame_length
        ):

            frame = vad_buffer[
                :vad_frame_length
            ]

            del vad_buffer[
                :vad_frame_length
            ]

            frame_bytes = np.asarray(
                frame,
                dtype=np.int16
            ).tobytes()

            try:

                is_speech = vad.is_speech(
                    frame_bytes,
                    TARGET_SAMPLE_RATE
                )

            except Exception as exc:

                print(
                    f"[VAD] Errore: {exc}"
                )

                is_speech = True

            if is_speech:
                speech_detected = True

        return speech_detected

    # ========================================================
    # REFRESH LISTENER
    # ========================================================

    def refresh_listener() -> bool:
        """
        Rigenera completamente solo la parte wake-word:

        - chiude PyAudio stream
        - ricrea openWakeWord
        - riapre PyAudio stream

        Vosk, fuzzy parser e Home Assistant
        continuano a funzionare normalmente.
        """

        nonlocal stream
        nonlocal model
        nonlocal model_created_at
        nonlocal refresh_pending

        print()
        print(
            "[OPENWAKEWORD] "
            "=============================="
        )

        print(
            "[OPENWAKEWORD] "
            "Refresh completo listener"
        )

        print(
            "[OPENWAKEWORD] "
            "=============================="
        )

        try:

            # ------------------------------------------------
            # MICROPHONE CLOSE
            # ------------------------------------------------

            close_microphone(
                stream
            )

            stream = None

            # Piccolo intervallo per rilasciare ALSA/USB.
            time.sleep(
                0.25
            )

            # ------------------------------------------------
            # MODEL
            # ------------------------------------------------

            model = create_wake_model()

            # ------------------------------------------------
            # MICROPHONE
            # ------------------------------------------------

            stream = open_microphone()

            # ------------------------------------------------
            # TIME
            # ------------------------------------------------

            model_created_at = (
                time.monotonic()
            )

            refresh_pending = False

            # ------------------------------------------------
            # RESET STATE
            # ------------------------------------------------

            enter_listening(
                "listener rigenerato"
            )

            print(
                "[OPENWAKEWORD] "
                "Refresh completato"
            )

            print()

            return True

        except Exception as exc:

            print(
                "[OPENWAKEWORD] "
                f"Errore refresh: {exc}"
            )

            # Proviamo a recuperare almeno
            # lo stream audio.
            try:

                close_microphone(
                    stream
                )

                stream = None

                time.sleep(
                    1
                )

                stream = open_microphone()

                model = create_wake_model()

                model_created_at = (
                    time.monotonic()
                )

                refresh_pending = False

                enter_listening(
                    "recovery listener"
                )

                print(
                    "[OPENWAKEWORD] "
                    "Recovery completato"
                )

                return True

            except Exception as recovery_exc:

                print(
                    "[OPENWAKEWORD] "
                    "ERRORE recovery listener: "
                    f"{recovery_exc}"
                )

                stop_event.set()

                return False

    # ========================================================
    # INITIAL STATE
    # ========================================================

    enter_listening(
        "avvio"
    )

    # ========================================================
    # MAIN LOOP
    # ========================================================

    try:

        while not stop_event.is_set():

            # =================================================
            # PERIODIC REFRESH CHECK
            # =================================================

            now = time.monotonic()

            listener_age = (
                now
                - model_created_at
            )

            if (
                listener_refresh_seconds > 0
                and listener_age
                >= listener_refresh_seconds
            ):

                refresh_pending = True

            # Refresh solo quando non stiamo
            # registrando o aspettando un comando.
            if (
                refresh_pending
                and state == STATE_LISTENING
            ):

                if not refresh_listener():
                    break

                continue

            # =================================================
            # READ AUDIO
            # =================================================

            try:

                pcm_np = read_audio_chunk(
                    stream
                )

            except Exception as exc:

                print(
                    "[OPENWAKEWORD] "
                    "Errore lettura microfono: "
                    f"{exc}"
                )

                print(
                    "[OPENWAKEWORD] "
                    "Forzo refresh listener"
                )

                if not refresh_listener():
                    break

                continue

            now = time.monotonic()

            # =================================================
            # OPENWAKEWORD
            #
            # IMPORTANTE:
            # il modello viene alimentato SEMPRE.
            # =================================================

            try:

                prediction = model.predict(
                    pcm_np
                )

                score = prediction.get(
                    model_name,
                    0
                )

            except Exception as exc:

                print(
                    "[OPENWAKEWORD] "
                    "Errore predict: "
                    f"{exc}"
                )

                print(
                    "[OPENWAKEWORD] "
                    "Forzo refresh listener"
                )

                if not refresh_listener():
                    break

                continue

            # =================================================
            # COOLDOWN
            # =================================================

            if state == STATE_COOLDOWN:

                if (
                    cooldown_until is not None
                    and now >= cooldown_until
                ):

                    enter_listening(
                        "cooldown terminato"
                    )

                continue

            # =================================================
            # LISTENING
            # =================================================

            if state == STATE_LISTENING:

                if score >= 0.05:

                    print(
                        "[OPENWAKEWORD] "
                        f"score={score:.3f}"
                    )

                if score < threshold:
                    continue

                print()

                print(
                    "[LISTENER] "
                    "Wake word rilevata! "
                    f"score={score:.3f}"
                )

                print()

                # =============================================
                # BEEP
                # =============================================

                play_beep()

                # =============================================
                # WAIT COMMAND
                # =============================================

                clear_buffers()

                wait_command_start = (
                    time.monotonic()
                )

                state = STATE_WAIT_COMMAND

                print(
                    "[LISTENER] "
                    "Attendo comando..."
                )

                continue

            # =================================================
            # WAIT COMMAND
            # =================================================

            if state == STATE_WAIT_COMMAND:

                audio_buffer.extend(
                    pcm_np.tolist()
                )

                vad_buffer.extend(
                    pcm_np.tolist()
                )

                voice_detected = (
                    process_vad_frames()
                )

                if voice_detected:

                    now = time.monotonic()

                    command_start_time = now
                    last_voice_time = now

                    state = STATE_RECORDING

                    print(
                        "[LISTENER] "
                        "Inizio registrazione comando"
                    )

                    continue

                if wait_command_start is None:

                    enter_cooldown(
                        "stato WAIT non valido"
                    )

                    continue

                elapsed_wait = (
                    now
                    - wait_command_start
                )

                if (
                    elapsed_wait
                    >= vad_voice_start_timeout
                ):

                    print(
                        "[LISTENER] "
                        "Nessuna voce dopo wake word"
                    )

                    enter_cooldown(
                        "nessun comando"
                    )

                continue

            # =================================================
            # RECORDING
            # =================================================

            if state == STATE_RECORDING:

                audio_buffer.extend(
                    pcm_np.tolist()
                )

                vad_buffer.extend(
                    pcm_np.tolist()
                )

                speech_detected = (
                    process_vad_frames()
                )

                if speech_detected:

                    last_voice_time = now

                # =============================================
                # STATE SAFETY
                # =============================================

                if (
                    command_start_time is None
                    or last_voice_time is None
                ):

                    print(
                        "[LISTENER] "
                        "Stato registrazione non valido"
                    )

                    enter_cooldown(
                        "reset sicurezza"
                    )

                    continue

                # =============================================
                # TIMERS
                # =============================================

                command_elapsed = (
                    now
                    - command_start_time
                )

                silence_elapsed = (
                    now
                    - last_voice_time
                )

                silence_timeout = (
                    silence_elapsed
                    >= vad_voice_end_sec
                )

                command_timeout = (
                    command_elapsed
                    >= max_command_seconds
                )

                if (
                    not silence_timeout
                    and not command_timeout
                ):

                    continue

                # =============================================
                # END COMMAND
                # =============================================

                if command_timeout:

                    print(
                        "[LISTENER] "
                        "Timeout massimo comando "
                        f"({command_elapsed:.2f}s)"
                    )

                else:

                    print(
                        "[LISTENER] "
                        "Fine registrazione "
                        f"(silenzio "
                        f"{silence_elapsed:.2f}s)"
                    )

                # =============================================
                # POST BUFFER
                # =============================================

                post_samples = int(
                    post_buffer_seconds
                    * TARGET_SAMPLE_RATE
                )

                post_buffer: List[int] = []

                while (
                    len(post_buffer)
                    < post_samples
                    and not stop_event.is_set()
                ):

                    pcm_post = read_audio_chunk(
                        stream
                    )

                    # Manteniamo openWakeWord alimentato.
                    model.predict(
                        pcm_post
                    )

                    post_buffer.extend(
                        pcm_post.tolist()
                    )

                audio_buffer.extend(
                    post_buffer
                )

                # =============================================
                # DURATION
                # =============================================

                duration = (
                    len(audio_buffer)
                    / TARGET_SAMPLE_RATE
                )

                print(
                    "[LISTENER] "
                    f"Audio comando: "
                    f"{duration:.2f}s"
                )

                # =============================================
                # SEND TO VOSK
                # =============================================

                min_samples = int(
                    min_command_seconds
                    * TARGET_SAMPLE_RATE
                )

                if (
                    len(audio_buffer)
                    >= min_samples
                ):

                    buffer_to_send = list(
                        audio_buffer
                    )

                    audio_queue.put(
                        (
                            buffer_to_send,
                            TARGET_SAMPLE_RATE
                        )
                    )

                    print(
                        "[LISTENER] "
                        "Buffer inviato a Vosk"
                    )

                    if save_debug:

                        save_debug_audio(
                            buffer_to_send,
                            TARGET_SAMPLE_RATE
                        )

                else:

                    print(
                        "[LISTENER] "
                        "Audio troppo corto, ignoro"
                    )

                # =============================================
                # COOLDOWN
                # =============================================

                enter_cooldown(
                    "comando completato"
                )

                continue

            # =================================================
            # UNKNOWN STATE
            # =================================================

            print(
                "[LISTENER] "
                f"Stato sconosciuto: {state}"
            )

            enter_cooldown(
                "reset stato sconosciuto"
            )

    # ========================================================
    # FATAL ERROR
    # ========================================================

    except Exception as exc:

        print(
            "[OPENWAKEWORD] "
            f"Errore listener: {exc}"
        )

        stop_event.set()

    # ========================================================
    # CLEANUP
    # ========================================================

    finally:

        print(
            "[OPENWAKEWORD] "
            "Chiusura listener"
        )

        close_microphone(
            stream
        )

        try:
            audio.terminate()

        except Exception:
            pass