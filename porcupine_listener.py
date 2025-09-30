import pvporcupine
import pyaudio
import struct
from collections import deque
import threading
import sys

# Voice Activity Detection
import webrtcvad

def porcupine_listener(audio_queue, stop_event: threading.Event, config: dict):
    # Timeout massimo per iniziare a parlare dopo la wake word (in secondi)
    VAD_VOICE_START_TIMEOUT = config.get('vad_voice_start_timeout', 1.5)  # Timeout in secondi per iniziare a parlare dopo la wake word
    import wave
    import os
    import time

    def save_debug_audio(buffer, sample_rate):
        debug_dir = "debug_audio"
        os.makedirs(debug_dir, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        wav_path = os.path.join(debug_dir, f"command_{timestamp}.wav")
        with wave.open(wav_path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit audio
            wf.setframerate(sample_rate)
            wf.writeframes(struct.pack("<" + "h"*len(buffer), *buffer))
        print(f"[DEBUG] Audio comando salvato: {wav_path}")
    """
    Thread Porcupine che cattura audio dal microfono e lo invia
    alla coda per la trascrizione da Vosk.
    """
    ACCESS_KEY = config["access_key"]
    KEYWORDS = config["keywords"]
    SENSITIVITY = config.get("sensitivity", 0.7)
    PRE_BUFFER_SECONDS = config.get("pre_buffer_seconds", 0.5)
    SAVE_DEBUG_AUDIO = config.get("save_debug_audio", False)
    POST_BUFFER_SECONDS = config.get("post_buffer_seconds", 0.3)  # New: add trailing audio after silence
    WAKEWORD_TRIM_MS = config.get("wakeword_trim_ms", 300)
    SILENCE_THRESHOLD = config.get("silence_threshold", 500)
    SILENCE_DURATION = config.get("silence_duration", 2)

    print("[PORCUPINE] Thread partito")

    try:
        porcupine = pvporcupine.create(
            access_key=ACCESS_KEY,
            keywords=KEYWORDS,
            sensitivities=[SENSITIVITY]
        )
        pa = pyaudio.PyAudio()
        stream = pa.open(
            rate=porcupine.sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=porcupine.frame_length
        )
        print("[PORCUPINE] Listener pronto")
    except Exception as e:
        print(f"[PORCUPINE] Errore inizializzazione: {e}")
        return

    pre_buffer = deque(maxlen=int(PRE_BUFFER_SECONDS * porcupine.sample_rate))
    audio_buffer = []

    recording = False
    frame_duration = porcupine.frame_length / porcupine.sample_rate
    vad = webrtcvad.Vad()
    vad.set_mode(config.get('vad_mode', 2))  # 0-3, 3 = most aggressive
    voice_inactive_frames = 0
    max_voice_inactive_frames = int(config.get('vad_voice_end_sec', 0.5) / frame_duration)
    # Per VAD: accumula campioni per frame da 30ms (480 campioni a 16kHz)
    vad_frame_ms = 30
    vad_frame_length = int((vad_frame_ms / 1000) * porcupine.sample_rate)
    vad_buffer = []

    try:
        post_buffer = []
        post_buffer_frames = int(POST_BUFFER_SECONDS * porcupine.sample_rate)
        post_buffer_count = 0
        while not stop_event.is_set():
            pcm = stream.read(porcupine.frame_length, exception_on_overflow=False)
            pcm_unpacked = struct.unpack_from("h" * porcupine.frame_length, pcm)
            pre_buffer.extend(pcm_unpacked)

            # Wake word rilevata
            if porcupine.process(pcm_unpacked) >= 0 and not recording:
                print("[LISTENER] Wake word rilevata!")
                play_beep()
                recording = True
                samples_to_trim = int((WAKEWORD_TRIM_MS / 1000) * porcupine.sample_rate)
                pre_buffer_list = list(pre_buffer)
                pre_buffer_list = pre_buffer_list[samples_to_trim:] if samples_to_trim < len(pre_buffer_list) else []
                audio_buffer.extend(pre_buffer_list)
                voice_inactive_frames = 0
                vad_buffer = []
                # Timeout per inizio voce dopo wake word
                voice_detected = False
                start_time = time.time()
                while not voice_detected and (time.time() - start_time) < VAD_VOICE_START_TIMEOUT and not stop_event.is_set():
                    pcm_wait = stream.read(porcupine.frame_length, exception_on_overflow=False)
                    pcm_wait_unpacked = struct.unpack_from('h' * porcupine.frame_length, pcm_wait)
                    audio_buffer.extend(pcm_wait_unpacked)
                    vad_buffer.extend(pcm_wait_unpacked)
                    while len(vad_buffer) >= vad_frame_length:
                        vad_frame = vad_buffer[:vad_frame_length]
                        vad_bytes = struct.pack('<' + 'h'*vad_frame_length, *vad_frame)
                        try:
                            is_speech = vad.is_speech(vad_bytes, porcupine.sample_rate)
                        except Exception as e:
                            print(f"[VAD] Errore: {e}")
                            is_speech = True
                        if is_speech:
                            voice_detected = True
                            break
                        vad_buffer = vad_buffer[vad_frame_length:]
                if not voice_detected:
                    print(f"[LISTENER] Nessuna voce rilevata entro {VAD_VOICE_START_TIMEOUT}s dopo la wake word. Annulla registrazione.")
                    audio_buffer = []
                    recording = False
                    vad_buffer = []
                    pre_buffer.clear()
                    continue

            if recording:
                audio_buffer.extend(pcm_unpacked)
                # Accumula campioni per frame VAD da 30ms
                vad_buffer.extend(pcm_unpacked)
                while len(vad_buffer) >= vad_frame_length:
                    vad_frame = vad_buffer[:vad_frame_length]
                    vad_bytes = struct.pack('<' + 'h'*vad_frame_length, *vad_frame)
                    try:
                        is_speech = vad.is_speech(vad_bytes, porcupine.sample_rate)
                    except Exception as e:
                        print(f"[VAD] Errore: {e}")
                        is_speech = True  # fallback: non fermare la registrazione
                    if is_speech:
                        voice_inactive_frames = 0
                    else:
                        voice_inactive_frames += 1
                    # Rimuovi il frame appena processato
                    vad_buffer = vad_buffer[vad_frame_length:]

                if voice_inactive_frames >= max_voice_inactive_frames:
                    print("[LISTENER] Fine registrazione, voce terminata (VAD)")
                    post_buffer = []
                    post_buffer_count = 0
                    # Start collecting post-silence frames
                    while post_buffer_count < post_buffer_frames and not stop_event.is_set():
                        pcm_post = stream.read(porcupine.frame_length, exception_on_overflow=False)
                        pcm_post_unpacked = struct.unpack_from("h" * porcupine.frame_length, pcm_post)
                        post_buffer.extend(pcm_post_unpacked)
                        post_buffer_count += porcupine.frame_length
                    audio_buffer.extend(post_buffer)
                    # Se il buffer contiene solo silenzio (nessuna voce rilevata), non inviare a Vosk
                    min_voice_samples = int(0.2 * porcupine.sample_rate)  # almeno 0.2s di voce
                    if len(audio_buffer) < min_voice_samples:
                        print("[LISTENER] Nessuna voce rilevata dopo la wake word. Ignoro e riparto.")
                    else:
                        # INVIO BUFFER ALLA CODA: copia indipendente
                        buffer_to_send = audio_buffer[:]
                        audio_queue.put((buffer_to_send, porcupine.sample_rate))
                        print("[VOLK] Nuova registrazione: buffer inviato e azzerato")
                        # Salva audio solo se richiesto
                        if SAVE_DEBUG_AUDIO:
                            save_debug_audio(buffer_to_send, porcupine.sample_rate)
                    # RESET COMPLETO DEL BUFFER
                    audio_buffer = []
                    recording = False
                    voice_inactive_frames = 0
                    vad_buffer = []  # Pulisci sempre il buffer VAD
                    pre_buffer.clear()
    finally:
        stop_event.set()
        stream.stop_stream()
        stream.close()
        pa.terminate()
        porcupine.delete()
        print("[PORCUPINE] Listener terminato")

import numpy as np
try:
    import simpleaudio as sa
    def play_beep():
        # Generate a 1000 Hz sine wave for 200 ms
        frequency = 1000  # Hz
        duration = 0.2  # seconds
        sample_rate = 44100
        t = np.linspace(0, duration, int(sample_rate * duration), False)
        tone = np.sin(frequency * 2 * np.pi * t)
        audio = (tone * 32767).astype(np.int16)
        play_obj = sa.play_buffer(audio, 1, 2, sample_rate)
        play_obj.wait_done()
except ImportError:
    def play_beep():
        print("[WARN] simpleaudio non installato: beep non disponibile.")
