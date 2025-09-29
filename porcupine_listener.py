import pvporcupine
import pyaudio
import struct
from collections import deque
import threading
import sys

def porcupine_listener(audio_queue, stop_event: threading.Event, config: dict):
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
    silence_counter = 0
    frame_duration = porcupine.frame_length / porcupine.sample_rate

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

            if recording:
                audio_buffer.extend(pcm_unpacked)
                if max(pcm_unpacked) < SILENCE_THRESHOLD:
                    silence_counter += frame_duration
                else:
                    silence_counter = 0

                if silence_counter >= SILENCE_DURATION:
                    print("[LISTENER] Fine registrazione, pausa rilevata")
                    post_buffer = []
                    post_buffer_count = 0
                    # Start collecting post-silence frames
                    while post_buffer_count < post_buffer_frames and not stop_event.is_set():
                        pcm_post = stream.read(porcupine.frame_length, exception_on_overflow=False)
                        pcm_post_unpacked = struct.unpack_from("h" * porcupine.frame_length, pcm_post)
                        post_buffer.extend(pcm_post_unpacked)
                        post_buffer_count += porcupine.frame_length
                    audio_buffer.extend(post_buffer)
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
                    silence_counter = 0
                    pre_buffer.clear()
    finally:
        stop_event.set()
        stream.stop_stream()
        stream.close()
        pa.terminate()
        porcupine.delete()
        print("[PORCUPINE] Listener terminato")

if sys.platform == 'win32':
    import winsound
    def play_beep():
        winsound.Beep(1000, 200)
else:
    import os
    def play_beep():
        os.system('play -nq -t alsa synth 0.2 sine 1000')
