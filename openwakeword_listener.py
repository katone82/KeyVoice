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

# 80 ms a 16 kHz
OWW_FRAME_LENGTH = 1_280

# 80 ms a 48 kHz
DEVICE_FRAME_LENGTH = OWW_FRAME_LENGTH * 3


# ============================================================
# BEEP
# ============================================================

BEEP_FILE = "/home/homeassistant/KeyVoice/sounds/wake.wav"
BEEP_DEVICE = "plughw:3,0"


def play_beep() -> None:
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
# AUDIO UTILITIES
# ============================================================

def convert_48k_to_16k(
    pcm_bytes: bytes
) -> np.ndarray:

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


def read_audio_chunk(
    stream: pyaudio.Stream
) -> np.ndarray:

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

    # Tempo durante il quale il microfono viene letto,
    # ma openWakeWord NON viene interrogato.
    #
    # Serve ad evitare una riattivazione immediata
    # causata dalla coda del comando precedente.
    wakeword_cooldown_sec = config.get(
        "wakeword_cooldown_sec",
        1.2
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
        f"Cooldown wake word: "
        f"{wakeword_cooldown_sec}s"
    )

    # ========================================================
    # MODEL
    # ========================================================

    try:
        model = Model(
            wakeword_models=[
                model_name
            ],
            inference_framework="onnx"
        )

        print(
            "[OPENWAKEWORD] Modello caricato"
        )

    except Exception as exc:

        print(
            "[OPENWAKEWORD] "
            f"Errore caricamento modello: {exc}"
        )

        stop_event.set()
        return

    # ========================================================
    # MICROPHONE
    # ========================================================

    audio = pyaudio.PyAudio()
    stream = None

    try:
        device_info = (
            audio.get_device_info_by_index(
                INPUT_DEVICE_INDEX
            )
        )

        print(
            "[OPENWAKEWORD] Microfono: "
            f"{device_info['name']}"
        )

        print(
            "[OPENWAKEWORD] "
            f"Device index: {INPUT_DEVICE_INDEX}"
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

        stream = audio.open(
            rate=DEVICE_SAMPLE_RATE,
            channels=CHANNELS,
            format=AUDIO_FORMAT,
            input=True,
            input_device_index=INPUT_DEVICE_INDEX,
            frames_per_buffer=DEVICE_FRAME_LENGTH
        )

    except Exception as exc:

        print(
            "[OPENWAKEWORD] "
            f"Errore apertura microfono: {exc}"
        )

        audio.terminate()
        stop_event.set()

        return

    print(
        "[OPENWAKEWORD] Listener pronto"
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
    # STATE
    # ========================================================

    audio_buffer: List[int] = []
    vad_buffer: List[int] = []

    recording = False

    command_start_time = None
    last_voice_time = None

    cooldown_until = 0.0
    cooldown_logged = False

    # ========================================================
    # RESET
    # ========================================================

    def reset_to_listening(
        reason: str,
        apply_cooldown: bool = True
    ) -> None:

        nonlocal recording
        nonlocal command_start_time
        nonlocal last_voice_time
        nonlocal cooldown_until
        nonlocal cooldown_logged

        audio_buffer.clear()
        vad_buffer.clear()

        recording = False

        command_start_time = None
        last_voice_time = None

        if apply_cooldown:

            cooldown_until = (
                time.monotonic()
                + wakeword_cooldown_sec
            )

            cooldown_logged = False

        else:

            cooldown_until = 0.0
            cooldown_logged = True

        print(
            "[LISTENER] "
            f"Reset -> ascolto wake word "
            f"({reason})"
        )

    # ========================================================
    # VAD HELPER
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
    # MAIN LOOP
    # ========================================================

    try:

        while not stop_event.is_set():

            pcm_np = read_audio_chunk(
                stream
            )

            now = time.monotonic()

            # =================================================
            # COOLDOWN
            # =================================================

            if now < cooldown_until:

                # Continuiamo a leggere il microfono
                # così svuotiamo fisicamente l'audio residuo,
                # ma NON lo passiamo ad openWakeWord.

                continue

            if (
                cooldown_until > 0
                and not cooldown_logged
            ):

                print(
                    "[LISTENER] "
                    "Ascolto wake word riattivato"
                )

                cooldown_logged = True
                cooldown_until = 0.0

            # =================================================
            # WAIT WAKE WORD
            # =================================================

            if not recording:

                prediction = model.predict(
                    pcm_np
                )

                score = prediction.get(
                    model_name,
                    0
                )

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

                # =================================================
                # BEEP
                # =================================================

                play_beep()

                print(
                    "[LISTENER] Attendo comando..."
                )

                audio_buffer.clear()
                vad_buffer.clear()

                # =================================================
                # WAIT COMMAND START
                # =================================================

                voice_detected = False

                voice_wait_start = (
                    time.monotonic()
                )

                while (
                    not voice_detected
                    and not stop_event.is_set()
                ):

                    elapsed_wait = (
                        time.monotonic()
                        - voice_wait_start
                    )

                    if (
                        elapsed_wait
                        >= vad_voice_start_timeout
                    ):
                        break

                    pcm_wait = read_audio_chunk(
                        stream
                    )

                    audio_buffer.extend(
                        pcm_wait.tolist()
                    )

                    vad_buffer.extend(
                        pcm_wait.tolist()
                    )

                    if process_vad_frames():

                        voice_detected = True

                        now = time.monotonic()

                        command_start_time = now
                        last_voice_time = now

                # =================================================
                # NO COMMAND
                # =================================================

                if not voice_detected:

                    print(
                        "[LISTENER] "
                        "Nessuna voce dopo wake word"
                    )

                    reset_to_listening(
                        "nessun comando"
                    )

                    continue

                # =================================================
                # COMMAND START
                # =================================================

                print(
                    "[LISTENER] "
                    "Inizio registrazione comando"
                )

                recording = True

                continue

            # =================================================
            # RECORD COMMAND
            # =================================================

            audio_buffer.extend(
                pcm_np.tolist()
            )

            vad_buffer.extend(
                pcm_np.tolist()
            )

            speech_detected = (
                process_vad_frames()
            )

            now = time.monotonic()

            if speech_detected:

                last_voice_time = now

            # Sicurezza
            if (
                command_start_time is None
                or last_voice_time is None
            ):

                reset_to_listening(
                    "stato registrazione non valido"
                )

                continue

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

            max_timeout = (
                command_elapsed
                >= max_command_seconds
            )

            if (
                not silence_timeout
                and not max_timeout
            ):
                continue

            # =================================================
            # END COMMAND
            # =================================================

            if max_timeout:

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

            # =================================================
            # POST BUFFER
            # =================================================

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

                post_buffer.extend(
                    pcm_post.tolist()
                )

            audio_buffer.extend(
                post_buffer
            )

            duration = (
                len(audio_buffer)
                / TARGET_SAMPLE_RATE
            )

            print(
                "[LISTENER] "
                f"Audio comando: "
                f"{duration:.2f}s"
            )

            # =================================================
            # SEND TO VOSK
            # =================================================

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

            # =================================================
            # IMPORTANT: FORCE REARM
            # =================================================

            reset_to_listening(
                "comando completato"
            )

            continue

    except Exception as exc:

        print(
            "[OPENWAKEWORD] "
            f"Errore listener: {exc}"
        )

        stop_event.set()

    finally:

        print(
            "[OPENWAKEWORD] "
            "Chiusura listener"
        )

        if stream is not None:

            try:

                if stream.is_active():
                    stream.stop_stream()

            except Exception:
                pass

            try:

                stream.close()

            except Exception:
                pass

        audio.terminate()