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

# Il microfono USB supporta 44.1 kHz / 48 kHz.
# Utilizziamo 48 kHz perché il rapporto con 16 kHz è 3:1.
DEVICE_SAMPLE_RATE = 48_000

INPUT_DEVICE_INDEX = 0

CHANNELS = 1
AUDIO_FORMAT = pyaudio.paInt16

# openWakeWord lavora bene con frame da circa 80 ms:
#
# 16.000 Hz * 0.080 s = 1280 samples
#
OWW_FRAME_LENGTH = 1_280

# A 48 kHz dobbiamo leggere:
#
# 48.000 / 16.000 = 3
# 1280 * 3 = 3840 samples
#
DEVICE_FRAME_LENGTH = OWW_FRAME_LENGTH * 3


# ============================================================
# WAKE WORD BEEP
# ============================================================

BEEP_FILE = "/home/homeassistant/KeyVoice/sounds/wake.wav"

# Uscita audio ALSA.
# Sound Blaster Play! 4
BEEP_DEVICE = "plughw:3,0"


def play_beep() -> None:
    """
    Riproduce il beep di conferma dopo il riconoscimento
    della wake word.

    Il metodo è bloccante intenzionalmente:
    prima viene riprodotto il breve beep e subito dopo
    KeyVoice inizia ad aspettare il comando vocale.
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
# AUDIO UTILITIES
# ============================================================

def convert_48k_to_16k(
    pcm_bytes: bytes
) -> np.ndarray:
    """
    Converte audio PCM int16 mono da 48 kHz a 16 kHz.

    Restituisce un array numpy int16 pronto per:
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


def read_audio_chunk(
    stream: pyaudio.Stream
) -> np.ndarray:
    """
    Legge un chunk dal microfono a 48 kHz
    e lo converte automaticamente a 16 kHz.
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
    Salva il comando registrato come WAV,
    utile per debug di VAD / Vosk.
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

    vad_voice_start_timeout = config.get(
        "vad_voice_start_timeout",
        1.5
    )

    vad_voice_end_sec = config.get(
        "vad_voice_end_sec",
        0.4
    )

    post_buffer_seconds = config.get(
        "post_buffer_seconds",
        0.1
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

    # 0 = permissivo
    # 3 = molto aggressivo
    vad.set_mode(2)

    vad_frame_ms = 30

    vad_frame_length = int(
        TARGET_SAMPLE_RATE
        * vad_frame_ms
        / 1000
    )

    max_voice_inactive_frames = max(
        1,
        int(
            vad_voice_end_sec
            / (vad_frame_ms / 1000)
        )
    )

    # ========================================================
    # BUFFERS
    # ========================================================

    audio_buffer: List[int] = []
    vad_buffer: List[int] = []

    recording = False

    voice_inactive_frames = 0

    # ========================================================
    # VAD HELPER
    # ========================================================

    def process_vad_frames() -> bool:
        """
        Analizza tutti i frame da 30 ms presenti
        nel buffer VAD.

        Restituisce True se almeno un frame
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
                if vad.is_speech(
                    frame_bytes,
                    TARGET_SAMPLE_RATE
                ):
                    speech_detected = True

            except Exception as exc:
                print(
                    f"[VAD] Errore: {exc}"
                )

                # In caso di errore VAD,
                # meglio non troncare il comando.
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

                # Mostriamo solo score interessanti
                # per non riempire inutilmente il log.
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
                # RESET REGISTRAZIONE
                # =============================================

                audio_buffer.clear()
                vad_buffer.clear()

                voice_inactive_frames = 0

                # =============================================
                # WAIT FOR COMMAND START
                # =============================================

                voice_detected = False

                start_time = time.time()

                while (
                    not voice_detected
                    and not stop_event.is_set()
                    and (
                        time.time()
                        - start_time
                        < vad_voice_start_timeout
                    )
                ):

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

                # =============================================
                # NO VOICE
                # =============================================

                if not voice_detected:

                    print(
                        "[LISTENER] "
                        "Nessuna voce dopo wake word"
                    )

                    audio_buffer.clear()
                    vad_buffer.clear()

                    continue

                # =============================================
                # COMMAND STARTED
                # =============================================

                print(
                    "[LISTENER] "
                    "Inizio registrazione comando"
                )

                recording = True
                voice_inactive_frames = 0

                # Il primo audio del comando è già
                # presente in audio_buffer.
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

            if speech_detected:
                voice_inactive_frames = 0

            else:
                voice_inactive_frames += 1

            # =================================================
            # COMMAND STILL ACTIVE
            # =================================================

            if (
                voice_inactive_frames
                < max_voice_inactive_frames
            ):
                continue

            # =================================================
            # END COMMAND
            # =================================================

            print(
                "[LISTENER] "
                "Fine registrazione (VAD)"
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
                f"Audio comando: {duration:.2f}s"
            )

            # =================================================
            # SEND TO VOSK
            # =================================================

            min_command_seconds = 0.4

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
            # RESET
            # =================================================

            audio_buffer.clear()
            vad_buffer.clear()

            voice_inactive_frames = 0
            recording = False

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