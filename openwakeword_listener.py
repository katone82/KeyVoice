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

# Il microfono USB lavora correttamente a 48 kHz.
# openWakeWord, WebRTC VAD e Vosk lavorano a 16 kHz.
DEVICE_SAMPLE_RATE = 48_000

INPUT_DEVICE_INDEX = 0

CHANNELS = 1
AUDIO_FORMAT = pyaudio.paInt16

# openWakeWord lavora bene con blocchi da circa 80 ms.
#
# 16.000 Hz * 0.080 s = 1280 samples
#
OWW_FRAME_LENGTH = 1_280

# A 48 kHz:
#
# 1280 * 3 = 3840 samples
#
DEVICE_FRAME_LENGTH = OWW_FRAME_LENGTH * 3


# ============================================================
# BEEP CONFIGURATION
# ============================================================

BEEP_FILE = "/home/homeassistant/KeyVoice/sounds/wake.wav"

# Sound Blaster Play! 4
BEEP_DEVICE = "plughw:3,0"


# ============================================================
# BEEP
# ============================================================

def play_beep() -> None:
    """
    Riproduce il beep dopo il riconoscimento della wake word.

    È volutamente bloccante:
    il beep termina e subito dopo parte la finestra
    di ascolto del comando.
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
            "[BEEP] Timeout riproduzione beep"
        )

    except Exception as exc:
        print(
            f"[BEEP] Errore riproduzione: {exc}"
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

    Il risultato può essere passato direttamente a:
    - openWakeWord
    - WebRTC VAD
    - Vosk
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
    e restituisce immediatamente audio a 16 kHz.
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
    Salva il comando inviato a Vosk come file WAV.
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
    # CONFIGURATION
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

    # Tempo massimo concesso per iniziare a parlare
    # dopo il beep.
    vad_voice_start_timeout = config.get(
        "vad_voice_start_timeout",
        1.5
    )

    # Silenzio necessario per considerare
    # terminato il comando.
    vad_voice_end_sec = config.get(
        "vad_voice_end_sec",
        0.6
    )

    # Durata massima assoluta di un comando.
    # Impedisce registrazioni da 30-40 secondi.
    max_command_seconds = config.get(
        "max_command_seconds",
        6.0
    )

    # Piccolo margine registrato dopo
    # la fine del comando.
    post_buffer_seconds = config.get(
        "post_buffer_seconds",
        0.1
    )

    # Durata minima accettabile di un comando.
    min_command_seconds = config.get(
        "min_command_seconds",
        0.4
    )

    # Modalità WebRTC VAD:
    #
    # 0 = molto permissiva
    # 1 = permissiva
    # 2 = media
    # 3 = aggressiva
    #
    vad_mode = config.get(
        "vad_mode",
        2
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
        "[OPENWAKEWORD] "
        f"VAD mode: {vad_mode}"
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

    # ========================================================
    # OPENWAKEWORD MODEL
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
    # AUDIO DEVICE
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
            "[OPENWAKEWORD] Capture audio: "
            f"{DEVICE_SAMPLE_RATE} Hz"
        )

        print(
            "[OPENWAKEWORD] Processing audio: "
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
    # WEBRTC VAD
    # ========================================================

    vad = webrtcvad.Vad()

    vad.set_mode(
        vad_mode
    )

    # WebRTC VAD accetta frame da:
    # 10, 20 oppure 30 ms.
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

    recording = False

    command_start_time = None
    last_voice_time = None

    # ========================================================
    # VAD HELPER
    # ========================================================

    def process_vad_frames() -> bool:
        """
        Analizza tutti i frame da 30 ms presenti
        nel buffer.

        Restituisce True se almeno uno dei frame
        contiene voce.
        """

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

                # Preferiamo non troncare il comando
                # in caso di errore del VAD.
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

            # =================================================
            # WAITING FOR WAKE WORD
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

                # =============================================
                # WAKE WORD DETECTED
                # =============================================

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

                print(
                    "[LISTENER] Attendo comando..."
                )

                # =============================================
                # RESET
                # =============================================

                audio_buffer.clear()
                vad_buffer.clear()

                # =============================================
                # WAIT FOR VOICE START
                # =============================================

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

                # =============================================
                # NO COMMAND AFTER WAKE WORD
                # =============================================

                if not voice_detected:

                    print(
                        "[LISTENER] "
                        "Nessuna voce dopo wake word"
                    )

                    audio_buffer.clear()
                    vad_buffer.clear()

                    command_start_time = None
                    last_voice_time = None

                    continue

                # =============================================
                # START RECORDING
                # =============================================

                print(
                    "[LISTENER] "
                    "Inizio registrazione comando"
                )

                recording = True

                # Il primo pezzo parlato è già presente
                # in audio_buffer.
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

            # =================================================
            # TIMERS
            # =================================================

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

            # =================================================
            # COMMAND STILL ACTIVE
            # =================================================

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
                    f"raggiunto "
                    f"({command_elapsed:.2f}s)"
                )

            else:

                print(
                    "[LISTENER] "
                    "Fine registrazione "
                    f"(silenzio {silence_elapsed:.2f}s)"
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

            # =================================================
            # COMMAND DURATION
            # =================================================

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
            # RESET FOR NEXT WAKE WORD
            # =================================================

            audio_buffer.clear()
            vad_buffer.clear()

            recording = False

            command_start_time = None
            last_voice_time = None

    # ========================================================
    # ERROR
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