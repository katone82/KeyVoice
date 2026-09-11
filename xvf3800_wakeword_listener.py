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

import json

import numpy as np
import sounddevice as sd
import vosk

from vosk_listener import normalize_text

import debug_config

# ============================================================
# CONFIGURAZIONE
# ============================================================

TARGET_SAMPLE_RATE = 16000
DEVICE_SAMPLE_RATE = 16000
CHANNELS = 2

# Canale usato SOLO per la wake word (RMS/direzione/Vosk
# continuano a usare il canale 0). Per default firmware
# (documentazione ufficiale XMOS/reSpeaker, nessun comando al
# chip necessario) il canale sinistro (0) è l'uscita
# "communication": beamforming + AEC + post-process non lineare
# (noise suppression/limiter aggressivi, pensati per l'ascolto
# umano). Il canale destro (1) è invece l'uscita ASR del beam
# auto-selezionato: il chip mantiene un beam libero che scandisce
# l'ambiente, individua la sorgente dominante e ci punta uno dei
# beam focalizzati, selezionando automaticamente il segnale
# migliore — e NON passa per il post-process non lineare, perché
# quel tipo di elaborazione danneggia (non aiuta) un motore
# ASR/wake-word. In ambienti rumorosi o con voce debole/distante,
# il canale 1 è quindi il candidato giusto per la wake word:
# prova a metterlo a 1 (wake_audio_channel in config.json) e
# confronta gli score in [WAKE-DEBUG] a parità di distanza/voce.
WAKE_AUDIO_CHANNEL = 0

# ------------------------------------------------------------
# MOTORE WAKE WORD: VOSK A GRAMMATICA CHIUSA
# ------------------------------------------------------------
#
# Riusa il motore ASR già in uso nel progetto per trascrivere i
# comandi (vosk_listener.py), con una grammatica ristretta alla
# sola frase di attivazione + il token "[unk]" per tutto il resto
# — stessa tecnica già impiegata in vosk_listener.py per i comandi
# domotici (entities/azioni, vedi build_vosk_grammar()). La frase
# passa per normalize_text() esattamente come le entities, per
# coerenza. Un motore ASR pieno è, per costruzione, più robusto al
# rumore/sorgenti concorrenti rispetto a un modello wake-word
# dedicato piccolo, a costo di più CPU.
#
# NOTA: carica un SECONDO modello Vosk in memoria, indipendente da
# quello usato da vosk_listener.py per i comandi (i due thread non
# condividono nulla). Se il modello configurato è pesante,
# valutare la condivisione di un'unica istanza vosk.Model tra i
# due thread invece di caricarla due volte.
VOSK_MODEL_PATH = ""

VOSK_WAKE_PHRASE = "hey jarvis"

# Dopo la fine di un comando (reset_to_listening), per questa
# finestra la wake word viene ignorata anche se riconosciuta:
# subito dopo un comando (click del relè di uno switch, coda del
# beep, rumore residuo dalla stessa direzione appena bloccata) è
# più probabile un innesco spurio. Una vera ripetizione della
# wake word arriva comunque, solo qualche secondo più tardi.
WAKE_POST_COMMAND_STRICT_SECONDS = 2.0

# Battito cardiaco: una riga leggera ogni tot secondi mentre si
# è in ascolto, per confermare che il processo è vivo anche con
# debug_config.DEBUG_LOGGING spento (utile per distinguere "in ascolto in
# silenzio" da "bloccato per davvero").
ALIVE_LOG_INTERVAL = 15.0

# Stampa periodicamente cosa sta sentendo il recognizer Vosk
# (testo parziale/finale), anche quando non è la frase di
# attivazione — utile per capire se il problema è il riconoscimento
# o l'audio in ingresso.
WAKE_DEBUG_INTERVAL = 1.0

# ------------------------------------------------------------
# REGISTRAZIONE DEBUG "SCATOLA NERA" (opzionale)
# ------------------------------------------------------------
#
# Se attiva, tiene sempre in memoria gli ultimi WAKE_DEBUG_AUDIO_
# SECONDS secondi dell'audio POST-AGC sul canale usato per la
# wake word (esattamente quello che arriva al recognizer Vosk) e
# li scrive periodicamente su file WAV. Serve a rispondere alla
# domanda "il modello non sente bene, o l'audio che gli arriva è
# già di per sé poco chiaro?" senza dover indovinare dai soli
# numeri di score/peak nei log: dopo un tentativo fallito, basta
# riascoltare il file per capire se il problema è a monte (audio)
# o nel modello stesso.
#
# Disattivata di default: scrive su disco (SD card) ogni pochi
# secondi, quindi va accesa solo per diagnosticare e poi spenta.
WAKE_DEBUG_AUDIO_ENABLED = False

WAKE_DEBUG_AUDIO_SECONDS = 4.0

WAKE_DEBUG_AUDIO_PATH = "/tmp/keyvoice_wake_debug.wav"

WAKE_DEBUG_AUDIO_WRITE_INTERVAL = 1.0

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
# Ridotto da 0.5 a 0.25s: un pre-roll piu' corto riduce le
# occasioni di catturare rumore ambientale (TV, click, coda del
# beep) prima della voce vera, che Vosk — grammatica chiusa,
# nessun "cestino" per il rumore — è costretto a interpretare
# come una parola nota, anteponendola al comando reale (es.
# "alarm accendi luce tavolo"). trim_leading_noise() fa comunque
# da rete di sicurezza per l'attacco morbido della voce.
PRE_ROLL_SECONDS = 0.25

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

# Richiede anche il flag "speech" letto da DOA_VALUE (vedi
# XVF3800Telemetry, modulo GPO/LED del chip, resid 20) prima di
# iniziare a registrare un comando in process_wait_command(): un
# segnale indipendente dall'energia per beam di
# AEC_SPENERGY_VALUES gia' usata per la dominanza direzionale.
# Se il device/firmware non espone DOA_VALUE, il gate e'
# fail-open (si comporta come se fosse disattivato) quindi
# lasciarlo a True qui non rischia di rompere nulla su un
# firmware che non lo supporta. Metti a False per disattivarlo
# esplicitamente se in pratica risultasse inaffidabile.
WAKE_HW_SPEECH_GATE_ENABLED = True

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
# BEAM FISSO (opzionale)
# ------------------------------------------------------------
#
# Di default il canale USB usato per la wake word (WAKE_AUDIO_
# CHANNEL) porta l'uscita del beam "auto-selezionato": il chip
# sceglie in autonomia, momento per momento, quale beam è il
# migliore. Con una seconda sorgente sonora vicina in angolo e
# comparabile per energia, l'auto-select può saltare avanti e
# indietro fra le due sorgenti nell'arco di pochi secondi — con
# quell'audio che salta da una sorgente all'altra a metà frase,
# la wake word non riesce mai ad accumulare un tratto continuo e
# pulito della voce giusta (anche quando quest'ultima è ben
# udibile).
#
# Se chi parla resta sempre più o meno nella stessa posizione,
# la soluzione è puntare un BEAM FISSO su quell'angolo e
# instradare quel beam (non l'auto-select) sul canale USB, cosa
# che il chip supporta nativamente (nessuna elaborazione nostra):
# l'uscita resta agganciata a quella direzione indipendentemente
# da cosa succede altrove nella stanza. Instradato sulla
# categoria "AEC residual/ASR" (non sul "communication" col
# post-processor non lineare) perché quest'ultimo degrada la
# resa di un motore ASR/wake-word.
#
# Disattivato di default: se il firmware/device non supporta
# questi comandi o li rifiuta, la configurazione fallisce in modo
# innocuo (loggata, il resto del sistema continua a funzionare
# con l'auto-select come prima).
XVF_FIXED_BEAM_ENABLED = False

# Angolo (gradi, stessa convenzione di DOM/DOA nei log [XVF]) su
# cui puntare il beam fisso 1. Il beam fisso 2 viene puntato sullo
# stesso angolo (non lo usiamo: instradiamo solo il beam 1), non è
# un valore critico.
XVF_FIXED_BEAM_AZIMUTH_DEGREES = 0.0

# Quando abilitato, il beam fisso viene silenziato nei momenti in
# cui non c'è energia vocale sulla sua direzione, invece di
# lasciar passare riverbero/coda della sorgente concorrente.
XVF_FIXED_BEAM_GATING = True

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
# CONFIG DA config.json (opzionale)
# ============================================================

def apply_config(cfg):
    """
    Sovrascrive le costanti di modulo con eventuali override
    dalla sezione "openwakeword" di config.json. Ogni chiave
    assente in cfg lascia invariato il default hardcoded qui
    sopra. Va chiamata da run_service.py DOPO aver caricato
    CONFIG e PRIMA di istanziare WakeWordListener — le funzioni
    di questo modulo leggono queste costanti a runtime, quindi
    vedono correttamente il valore aggiornato qui (sono nello
    stesso modulo: a differenza di un "from modulo import nome"
    fatto da un ALTRO file, qui non c'è alcun valore "congelato"
    all'import).

    Esempio in config.json:

        "openwakeword": {
            "vosk_model_path": "/home/homeassistant/vosk-model/vosk-model-small-it-0.22",
            "vosk_wake_phrase": "hey jarvis",
            "post_command_strict_seconds": 2.0,
            "pre_roll_seconds": 0.25,
            "post_wake_ignore_seconds": 0.40,
            "command_timeout_seconds": 4.0,
            "max_command_seconds": 6.0,
            "speech_start_ratio": 2.5,
            "speech_end_ratio": 1.5,
            "speech_start_time": 0.03,
            "speech_end_time": 0.60,
            "direction_angle_tolerance": 60.0,
            "direction_lost_grace_seconds": 0.5,
            "direction_min_dominance": 1.2,
            "direction_min_energy": 1.0,
            "direction_confirmations": 3,
            "wake_direction_lookback_seconds": 1.0,
            "hw_speech_gate_enabled": true,
            "agc_max_gain": 8.0,
            "agc_silence_floor": 15,
            "wake_audio_channel": 0,
            "fixed_beam_enabled": false,
            "fixed_beam_azimuth_degrees": 0.0,
            "fixed_beam_gating": true,
            "wake_debug_audio_enabled": false,
            "wake_debug_audio_seconds": 4.0,
            "wake_debug_audio_path": "/tmp/keyvoice_wake_debug.wav",
            "beep_device": "plughw:3,0",
            "beep_file": "./sounds/wake.wav",
            "input_device_name": "reSpeaker XVF3800 4-Mic Array"
        }
    """

    global VOSK_MODEL_PATH, VOSK_WAKE_PHRASE
    global WAKE_POST_COMMAND_STRICT_SECONDS
    global PRE_ROLL_SECONDS, POST_WAKE_IGNORE_SECONDS
    global COMMAND_TIMEOUT_SECONDS, MAX_COMMAND_SECONDS
    global SPEECH_START_RATIO, SPEECH_END_RATIO, SPEECH_END_TIME
    global SPEECH_START_TIME
    global DIRECTION_ANGLE_TOLERANCE, DIRECTION_LOST_GRACE_SECONDS
    global DIRECTION_MIN_DOMINANCE, DIRECTION_MIN_ENERGY
    global DIRECTION_CONFIRMATIONS
    global WAKE_DIRECTION_LOOKBACK_SECONDS
    global WAKE_HW_SPEECH_GATE_ENABLED
    global WAKE_AGC_MAX_GAIN, WAKE_AGC_SILENCE_FLOOR
    global WAKE_AUDIO_CHANNEL
    global XVF_FIXED_BEAM_ENABLED, XVF_FIXED_BEAM_AZIMUTH_DEGREES
    global XVF_FIXED_BEAM_GATING
    global WAKE_DEBUG_AUDIO_ENABLED, WAKE_DEBUG_AUDIO_SECONDS
    global WAKE_DEBUG_AUDIO_PATH
    global BEEP_DEVICE, BEEP_FILE
    global INPUT_DEVICE_NAME

    if not cfg:
        return

    VOSK_MODEL_PATH = cfg.get(
        "vosk_model_path", VOSK_MODEL_PATH
    )
    VOSK_WAKE_PHRASE = cfg.get(
        "vosk_wake_phrase", VOSK_WAKE_PHRASE
    )

    WAKE_POST_COMMAND_STRICT_SECONDS = cfg.get(
        "post_command_strict_seconds",
        WAKE_POST_COMMAND_STRICT_SECONDS
    )

    PRE_ROLL_SECONDS = cfg.get(
        "pre_roll_seconds", PRE_ROLL_SECONDS
    )
    POST_WAKE_IGNORE_SECONDS = cfg.get(
        "post_wake_ignore_seconds", POST_WAKE_IGNORE_SECONDS
    )
    COMMAND_TIMEOUT_SECONDS = cfg.get(
        "command_timeout_seconds", COMMAND_TIMEOUT_SECONDS
    )
    MAX_COMMAND_SECONDS = cfg.get(
        "max_command_seconds", MAX_COMMAND_SECONDS
    )

    SPEECH_START_RATIO = cfg.get(
        "speech_start_ratio", SPEECH_START_RATIO
    )
    SPEECH_END_RATIO = cfg.get(
        "speech_end_ratio", SPEECH_END_RATIO
    )
    SPEECH_END_TIME = cfg.get(
        "speech_end_time", SPEECH_END_TIME
    )
    SPEECH_START_TIME = cfg.get(
        "speech_start_time", SPEECH_START_TIME
    )

    DIRECTION_ANGLE_TOLERANCE = cfg.get(
        "direction_angle_tolerance", DIRECTION_ANGLE_TOLERANCE
    )
    DIRECTION_LOST_GRACE_SECONDS = cfg.get(
        "direction_lost_grace_seconds",
        DIRECTION_LOST_GRACE_SECONDS
    )
    DIRECTION_MIN_DOMINANCE = cfg.get(
        "direction_min_dominance", DIRECTION_MIN_DOMINANCE
    )
    DIRECTION_MIN_ENERGY = cfg.get(
        "direction_min_energy", DIRECTION_MIN_ENERGY
    )
    DIRECTION_CONFIRMATIONS = cfg.get(
        "direction_confirmations", DIRECTION_CONFIRMATIONS
    )
    WAKE_DIRECTION_LOOKBACK_SECONDS = cfg.get(
        "wake_direction_lookback_seconds",
        WAKE_DIRECTION_LOOKBACK_SECONDS
    )

    WAKE_HW_SPEECH_GATE_ENABLED = cfg.get(
        "hw_speech_gate_enabled", WAKE_HW_SPEECH_GATE_ENABLED
    )

    WAKE_AGC_MAX_GAIN = cfg.get(
        "agc_max_gain", WAKE_AGC_MAX_GAIN
    )
    WAKE_AGC_SILENCE_FLOOR = cfg.get(
        "agc_silence_floor", WAKE_AGC_SILENCE_FLOOR
    )

    WAKE_AUDIO_CHANNEL = cfg.get(
        "wake_audio_channel", WAKE_AUDIO_CHANNEL
    )

    XVF_FIXED_BEAM_ENABLED = cfg.get(
        "fixed_beam_enabled", XVF_FIXED_BEAM_ENABLED
    )
    XVF_FIXED_BEAM_AZIMUTH_DEGREES = cfg.get(
        "fixed_beam_azimuth_degrees",
        XVF_FIXED_BEAM_AZIMUTH_DEGREES
    )
    XVF_FIXED_BEAM_GATING = cfg.get(
        "fixed_beam_gating", XVF_FIXED_BEAM_GATING
    )

    WAKE_DEBUG_AUDIO_ENABLED = cfg.get(
        "wake_debug_audio_enabled", WAKE_DEBUG_AUDIO_ENABLED
    )
    WAKE_DEBUG_AUDIO_SECONDS = cfg.get(
        "wake_debug_audio_seconds", WAKE_DEBUG_AUDIO_SECONDS
    )
    WAKE_DEBUG_AUDIO_PATH = cfg.get(
        "wake_debug_audio_path", WAKE_DEBUG_AUDIO_PATH
    )

    BEEP_DEVICE = cfg.get(
        "beep_device", BEEP_DEVICE
    )

    beep_file_override = cfg.get("beep_file")

    if beep_file_override:

        # Stessa logica di risoluzione usata sopra: percorso
        # relativo alla cartella dello script se non assoluto.
        if os.path.isabs(beep_file_override):
            BEEP_FILE = beep_file_override
        else:
            BEEP_FILE = os.path.abspath(
                os.path.join(SCRIPT_DIR, beep_file_override)
            )

        if not os.path.isfile(BEEP_FILE):
            print(
                f"[BEEP] ATTENZIONE: file beep da config.json "
                f"non trovato: {BEEP_FILE}"
            )

    INPUT_DEVICE_NAME = cfg.get(
        "input_device_name", INPUT_DEVICE_NAME
    )

    print(
        "[INIT] Configurazione da config.json applicata "
        f"({len(cfg)} chiavi)"
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

        # Flag di rilevamento vocale letto da DOA_VALUE (modulo
        # GPO/LED del chip, resid 20, cmdid 18 in xvf_host.py):
        # payload[0] = DoA in gradi (0-359), payload[1] = 1 se il
        # firmware rileva voce, 0 altrimenti. E' un segnale
        # indipendente da AEC_SPENERGY_VALUES (resid 33, modulo
        # AEC) usato sopra per dominant_energy/dominance_ratio:
        # quel valore e' "energia nel beam", questo e' un giudizio
        # booleano dedicato calcolato da un modulo diverso del
        # firmware. None finche' non abbiamo mai ricevuto una
        # lettura valida (device/firmware che non espone il
        # parametro): i consumer trattano None come "non
        # disponibile" e non bloccano nulla (fail-open).
        self.hw_doa_angle = None
        self.hw_speech_detected = None
        self.hw_speech_seen = False
        self.hw_speech_error_count = 0

        self.last_update = 0.0

        self.error_count = 0

        self.last_debug = 0.0

        # Storico (timestamp, angolo, energia, ratio, valido) per
        # poter recuperare, al momento della wake word, la
        # direzione dominante di qualche centinaio di ms prima.
        self.history = collections.deque(maxlen=200)

        # Storico (timestamp, angolo, speech_flag) del segnale
        # hardware DOA_VALUE/hw_speech_detected (quello che pilota
        # anche il LED del device). A differenza di self.history
        # (basata su AEC_SPENERGY_VALUES, cioè "qual è il beam più
        # energico in assoluto"), questo riflette il giudizio
        # dedicato del firmware su CHI sta parlando in questo
        # momento — utile per non farsi rubare il lock direzionale
        # da una seconda sorgente sonora più forte ma non "vocale"
        # nel senso riconosciuto dal chip. Vedi recent_hw_speech_
        # direction() sotto.
        self.hw_history = collections.deque(maxlen=200)

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

        if XVF_FIXED_BEAM_ENABLED:
            self._configure_fixed_beam()

    # --------------------------------------------------------

    def _configure_fixed_beam(self):
        """
        Punta il beam fisso 1 su XVF_FIXED_BEAM_AZIMUTH_DEGREES e
        instrada QUEL beam (non l'auto-select) sul canale USB
        usato per la wake word (WAKE_AUDIO_CHANNEL), sulla
        categoria "AEC residual/ASR" (7) invece che su
        "Processed"/communication (6) — quest'ultima passa dal
        post-processor non lineare che degrada la resa di un
        motore ASR/wake-word.

        Richiamata da _connect(): se il device si disconnette e
        _worker() riconnette, la configurazione viene riapplicata
        automaticamente (il chip non la persiste da solo tra le
        sessioni USB).

        Fallisce in modo innocuo (solo un log) se il firmware/
        device non supporta questi comandi: il resto del sistema
        continua a funzionare con il routing di default
        (auto-select) come se XVF_FIXED_BEAM_ENABLED fosse False.
        """

        try:

            azimuth_rad = math.radians(
                XVF_FIXED_BEAM_AZIMUTH_DEGREES
            )

            # AEC_FIXEDBEAMSAZIMUTH_VALUES vuole 2 valori (beam
            # fisso 1, beam fisso 2). Il beam 2 non viene
            # instradato su nessun canale che leggiamo: puntarlo
            # sullo stesso angolo del beam 1 è innocuo, serve solo
            # a evitare di lasciarlo sulla sua direzione di
            # default (che potrebbe non essere quella voluta).
            self.device.write(
                "AEC_FIXEDBEAMSAZIMUTH_VALUES",
                [azimuth_rad, azimuth_rad]
            )

            self.device.write(
                "AEC_FIXEDBEAMSONOFF", [1]
            )

            self.device.write(
                "AEC_FIXEDBEAMSGATING",
                [1 if XVF_FIXED_BEAM_GATING else 0]
            )

            # category 7 = AEC residual/ASR data; source 0 = beam
            # fisso 1 (stessa convenzione di indicizzazione beam
            # usata in AEC_AZIMUTH_VALUES/AEC_SPENERGY_VALUES: 0 =
            # beam1, 1 = beam2, 2 = beam libero, 3 = auto-select).
            op_param = (
                "AUDIO_MGR_OP_L"
                if WAKE_AUDIO_CHANNEL == 0
                else "AUDIO_MGR_OP_R"
            )

            self.device.write(op_param, [7, 0])

            print(
                "[XVF] Beam fisso attivato: "
                f"{XVF_FIXED_BEAM_AZIMUTH_DEGREES:.1f}° -> "
                f"canale {'L' if WAKE_AUDIO_CHANNEL == 0 else 'R'} "
                f"(gating="
                f"{'on' if XVF_FIXED_BEAM_GATING else 'off'})"
            )

        except Exception as e:

            print(
                f"[XVF] Errore configurazione beam fisso: {e} "
                "— continuo con il routing di default "
                "(auto-select)"
            )

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

        # DOA_VALUE non fa parte del modulo AEC (resid 33) come i
        # due sopra: e' del modulo GPO/LED (resid 20), quello che
        # pilota anche l'anello di LED sulla direzione di chi sta
        # parlando. Isolato nel proprio try/except: se questo
        # firmware/build non lo espone, non deve far fallire
        # anche la lettura di azimuth/energia sopra.
        try:
            hw_doa = self.device.read("DOA_VALUE")
        except Exception:
            hw_doa = None

        return list(azimuths), list(energies), hw_doa

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

                azimuths, energies, hw_doa = self._read()

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
                # HW SPEECH FLAG (DOA_VALUE, resid 20)
                #
                # Segnale indipendente da AEC_SPENERGY_VALUES: se
                # disponibile, aggiorniamo angolo+flag. Se questo
                # firmware/device non lo espone (hw_doa resta
                # None ad ogni ciclo), lasciamo semplicemente
                # hw_speech_detected a None per sempre: gli unici
                # punti che lo leggono (hw_speech_active) lo
                # trattano come "non disponibile" e non bloccano
                # mai nulla.
                # ------------------------------------------------

                if hw_doa is not None and len(hw_doa) >= 2:

                    if not self.hw_speech_seen:

                        self.hw_speech_seen = True

                        print(
                            "[XVF] DOA_VALUE disponibile: uso "
                            "anche il flag speech del chip "
                            "(modulo GPO, resid 20) come filtro "
                            "aggiuntivo indipendente dall'energia "
                            "per beam"
                        )

                    with self.lock:
                        self.hw_doa_angle = float(hw_doa[0])
                        self.hw_speech_detected = bool(hw_doa[1])

                        self.hw_history.append(
                            (
                                time.monotonic(),
                                self.hw_doa_angle,
                                self.hw_speech_detected
                            )
                        )

                elif (
                    not self.hw_speech_seen
                    and self.hw_speech_error_count <= 5
                ):

                    self.hw_speech_error_count += 1

                    if self.hw_speech_error_count == 5:

                        print(
                            "[XVF] DOA_VALUE non disponibile su "
                            "questo firmware/device dopo diversi "
                            "tentativi: il filtro extra sul flag "
                            "speech resta disattivato (fail-open, "
                            "nessun impatto sul funzionamento "
                            "esistente)"
                        )

                # ------------------------------------------------
                # DEBUG TELEMETRIA (solo se debug_config.DEBUG_LOGGING attivo)
                # ------------------------------------------------

                if debug_config.DEBUG_LOGGING:

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

                        hw_speech_text = (
                            "N/D"
                            if self.hw_speech_detected is None
                            else str(self.hw_speech_detected)
                        )

                        print(
                            f"[XVF] {beam_text} | "
                            f"DOM=B{dominant} "
                            f"{dominant_angle:.1f}° | "
                            f"2nd={second_energy:.0f} "
                            f"ratio={ratio:.2f} | "
                            f"hw_speech={hw_speech_text}"
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

                "hw_doa_angle":
                    self.hw_doa_angle,

                "hw_speech_detected":
                    self.hw_speech_detected,

                "last_update":
                    self.last_update
            }

    # --------------------------------------------------------

    def hw_speech_active(self):
        """
        Flag di voce rilevata letto da DOA_VALUE (modulo GPO/LED
        del chip, resid 20) — indipendente dall'energia per beam
        di AEC_SPENERGY_VALUES usata per dominant_energy/
        dominance_ratio sopra. Se il device/firmware non espone
        questo parametro (nessuna lettura valida ricevuta finora),
        ritorna sempre True per non bloccare mai il riconoscimento
        comandi (fail-open): il comportamento resta identico a
        prima dell'introduzione di questo filtro.
        """

        with self.lock:
            value = self.hw_speech_detected

        if value is None:
            return True

        return value

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
        """
        Vero se ALMENO UNO dei beam riportati dal device ha
        energia sufficiente ed è entro tolleranza da locked_angle.

        Prima guardava solo il beam dominante (il più energico in
        assoluto): con due sorgenti sonore contemporanee (una
        continua, l'altra la persona agganciata dalla wake word),
        se la sorgente continua è più forte diventa lei il beam
        dominante e questo controllo tornava sempre False sulla
        direzione giusta, anche con la persona che sta ancora
        parlando — troncando il comando o facendolo scadere in
        timeout. Scandire TUTTI i beam invece del solo dominante
        permette di riconoscere che c'è ancora energia vocale
        nella direzione agganciata anche quando non è la più forte
        della stanza in quel momento.
        """

        with self.lock:
            azimuths = list(self.azimuths)
            energies = list(self.energies)

        for angle, energy in zip(azimuths, energies):

            if energy < DIRECTION_MIN_ENERGY:
                continue

            distance = angle_distance(
                angle,
                locked_angle
            )

            if distance <= DIRECTION_ANGLE_TOLERANCE:
                return True

        return False

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

    # --------------------------------------------------------

    def recent_hw_speech_direction(self, window_seconds):
        """
        Come recent_direction(), ma cerca nello storico del flag
        hardware DOA_VALUE (self.hw_history) invece che in quello
        basato su AEC_SPENERGY_VALUES (self.history).

        Motivo per cui i due possono dare risposte diverse: con
        DUE sorgenti sonore contemporanee (es. una parla di
        continuo, l'altra pronuncia la wake word), recent_
        direction() sceglie la lettura più ENERGICA nella finestra
        — che finisce quasi sempre per essere la sorgente continua
        se è più forte, anche se non è lei ad aver detto la wake
        word. DOA_VALUE è invece un giudizio dedicato del firmware
        su CHI sta parlando in questo momento (lo stesso segnale
        che pilota il LED del device), quindi tra le letture
        valide preferiamo qui la più RECENTE con speech_flag=True
        invece della più energica: è la scelta giusta quando le
        due sorgenti competono, perché resta agganciata a chi ha
        effettivamente innescato la wake word invece che a chi
        parla più forte.

        Ritorna l'angolo (float) oppure None se nello storico non
        c'è nessuna lettura con voce rilevata nella finestra
        richiesta (es. device/firmware che non espone DOA_VALUE:
        hw_history resta sempre vuoto, fail-open verso il chiamante
        che ricade su recent_direction()).
        """

        now = time.monotonic()

        with self.lock:
            entries = [
                entry
                for entry in self.hw_history
                if now - entry[0] <= window_seconds
            ]

        # entry = (timestamp, angle, speech_flag)
        speech_entries = [
            entry for entry in entries if entry[2]
        ]

        if not speech_entries:
            return None

        most_recent = max(
            speech_entries,
            key=lambda entry: entry[0]
        )

        return most_recent[1]


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
        stop_event=None,
        ready_event=None,
        vosk_model=None
    ):

        # Coda esterna verso cui inoltrare l'audio del comando
        # catturato, nello stesso formato (buffer, sample_rate)
        # che vosk_listener.py si aspetta di ricevere. None in
        # modalità standalone (esecuzione diretta dello script).
        self.vosk_audio_queue = vosk_audio_queue

        # Istanza vosk.Model già caricata dal chiamante (vedi
        # run_service.py), condivisa con il recognizer dei comandi
        # in vosk_listener.py per non tenere due copie in RAM. Se
        # None (es. esecuzione standalone di questo script), la
        # carichiamo da soli più sotto.
        self._shared_vosk_model = vosk_model

        # threading.Event condiviso con gli altri thread di
        # run_service.py per uno spegnimento cooperativo. None in
        # modalità standalone: in quel caso resta l'unico modo per
        # uscire Ctrl+C (KeyboardInterrupt), gestito in run().
        self.stop_event = stop_event

        # threading.Event impostato quando il listener è
        # davvero operativo (device aperto, calibrazione rumore
        # completata), non solo quando il thread è partito.
        # run_service.py lo usa per stampare "pronto" solo a
        # inizializzazione conclusa.
        self.ready_event = ready_event

        print(
            f"[INIT] XVF host: "
            f"{XVF_HOST_PATH}"
        )

        print(
            "[INIT] Loading Vosk (motore wake word)..."
        )

        # I log interni di Vosk (livello INFO/WARNING della
        # libreria nativa) sono molto verbosi: silenziati come già
        # fa xvf_host per lo stesso motivo (XVF_SILENCE_VENDOR_
        # LOGS sopra). Impostazione globale della libreria, va
        # fatta comunque anche se il modello è condiviso.
        vosk.SetLogLevel(-1)

        if self._shared_vosk_model is not None:

            vosk_model = self._shared_vosk_model

            print(
                "[INIT] Vosk: uso il modello già caricato "
                "(condiviso con vosk_listener.py)"
            )

        else:

            if not VOSK_MODEL_PATH:
                raise RuntimeError(
                    "vosk_model_path non è impostato in "
                    "config.json (sezione \"openwakeword\") — "
                    "usa lo stesso percorso già impostato in "
                    "config[\"vosk\"][\"model_path\"]"
                )

            vosk_model = vosk.Model(VOSK_MODEL_PATH)

        # Stessa normalizzazione (minuscolo, senza accenti/
        # punteggiatura) applicata alle entities in build_vosk_
        # grammar() di vosk_listener.py — per coerenza e perché la
        # grammatica chiusa deve ricevere la frase esattamente
        # nella forma in cui Vosk la trascriverebbe.
        self.vosk_wake_phrase = normalize_text(
            VOSK_WAKE_PHRASE
        )

        grammar_json = json.dumps(
            [self.vosk_wake_phrase, "[unk]"],
            ensure_ascii=False
        )

        self.vosk_recognizer = vosk.KaldiRecognizer(
            vosk_model,
            TARGET_SAMPLE_RATE,
            grammar_json
        )

        self.vosk_recognizer.SetWords(False)

        print(
            "[INIT] Vosk (wake word) loaded "
            f"(frase='{self.vosk_wake_phrase}')"
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

        # Timestamp (time.monotonic) dell'ultima volta che siamo
        # tornati verso LISTENING dopo un comando (reset_to_listening).
        # Inizializzato a 0.0: all'avvio del processo non c'è
        # nessun comando precedente, quindi la finestra "strict"
        # in check_wakeword() risulta già scaduta fin da subito.
        self.command_finished_at = 0.0

        self.last_wake_debug = 0.0

        # Storico a rotazione dei blocchi POST-AGC (esattamente
        # quelli passati al recognizer Vosk) per la registrazione
        # di debug "scatola nera" — vedi WAKE_DEBUG_AUDIO_ENABLED.
        # maxlen in "numero di blocchi da BLOCK_SIZE (30ms)", non
        # in campioni: a differenza di un modello wake-word
        # dedicato, Vosk non impone una dimensione di chunk fissa,
        # quindi alimentiamo direttamente i blocchi così come
        # arrivano dal device.
        self.wake_debug_audio = collections.deque(
            maxlen=max(
                1,
                int(
                    WAKE_DEBUG_AUDIO_SECONDS
                    / (BLOCK_SIZE / TARGET_SAMPLE_RATE)
                )
            )
        )

        self.last_wake_debug_audio_write = 0.0

        # Evita che due scritture del file di debug si sovrappongano
        # se una scrittura su SD card fosse insolitamente lenta
        # (>1s): la successiva viene semplicemente saltata invece
        # di accodarsi, il rolling buffer si aggiorna comunque.
        self.wake_debug_audio_write_lock = threading.Lock()

        # Ultimo testo (parziale o finale) restituito dal
        # recognizer Vosk — usato per il debug periodico, mostra
        # cosa il motore sta davvero sentendo anche quando non è
        # la frase di attivazione.
        self.last_wake_text = ""

        # Punteggio "sintetico" (1.0 se rilevata in quest'ultimo
        # blocco, altrimenti 0.0) — Vosk in modalità a grammatica
        # chiusa non produce un punteggio continuo, ma teniamo
        # questo campo per uniformità con il resto del debug
        # periodico.
        self.last_wake_score = 0.0

        # Picco raw (pre-AGC) e guadagno applicato dall'ultimo
        # chunk processato, usati per il debug periodico: dicono
        # se il segnale in ingresso è debole e se l'AGC sta
        # davvero intervenendo.
        self.last_wake_peak = 0.0
        self.last_wake_gain = 1.0

        # Battito cardiaco leggero (una riga ogni ALIVE_LOG_INTERVAL
        # secondi) per confermare che il processo è vivo anche con
        # debug_config.DEBUG_LOGGING spento, senza inondare la console.
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
        """
        A differenza di un modello wake-word dedicato (che impone
        chunk di dimensione fissa e restituisce un punteggio
        continuo da sogliare), Vosk accetta blocchi di lunghezza
        qualsiasi e — in modalità a grammatica chiusa — restituisce
        sempre e solo una delle frasi della grammatica (qui: la
        frase di attivazione o "[unk]"). Alimentiamo quindi
        direttamente il blocco così come arriva dal device (30ms),
        senza bufferizzazione a parte.
        """

        mono = audio[:, WAKE_AUDIO_CHANNEL]

        in_strict_window = (
            time.monotonic() - self.command_finished_at
            < WAKE_POST_COMMAND_STRICT_SECONDS
        )

        chunk, peak, gain_applicato = normalize_wake_gain(
            mono
        )

        self.last_wake_peak = peak
        self.last_wake_gain = gain_applicato

        if WAKE_DEBUG_AUDIO_ENABLED:
            self.wake_debug_audio.append(chunk.copy())

        got_final = self.vosk_recognizer.AcceptWaveform(
            chunk.tobytes()
        )

        if got_final:

            result = json.loads(
                self.vosk_recognizer.Result()
            )

            text = result.get("text", "")

        else:

            result = json.loads(
                self.vosk_recognizer.PartialResult()
            )

            text = result.get("partial", "")

        self.last_wake_text = text

        detected = (
            bool(text)
            and self.vosk_wake_phrase in text
        )

        self.last_wake_score = 1.0 if detected else 0.0

        wake_detected = False

        if detected and not in_strict_window:

            wake_detected = True

            # Ricomincia da uno stato pulito: altrimenti il testo
            # già riconosciuto resterebbe nel buffer interno del
            # recognizer e potrebbe ri-matchare alla chiamata
            # successiva senza una nuova pronuncia.
            self.vosk_recognizer.Reset()

        now = time.monotonic()

        if (
            debug_config.DEBUG_LOGGING
            and now - self.last_wake_debug
            >= WAKE_DEBUG_INTERVAL
        ):

            self.last_wake_debug = now

            print(
                f"[WAKE-DEBUG] text='{self.last_wake_text}' "
                f"peak={self.last_wake_peak:.0f} "
                f"gain={self.last_wake_gain:.2f}x "
                f"strict={'Y' if in_strict_window else 'N'}"
            )

        if (
            WAKE_DEBUG_AUDIO_ENABLED
            and self.wake_debug_audio
            and now - self.last_wake_debug_audio_write
            >= WAKE_DEBUG_AUDIO_WRITE_INTERVAL
        ):

            self.last_wake_debug_audio_write = now

            self._write_wake_debug_audio()

        return wake_detected, self.last_wake_score

    # ========================================================
    # DEBUG: REGISTRAZIONE "SCATOLA NERA"
    # ========================================================

    def _write_wake_debug_audio(self):
        """
        Scrive su WAKE_DEBUG_AUDIO_PATH gli ultimi WAKE_DEBUG_
        AUDIO_SECONDS secondi di audio POST-AGC sul canale usato
        per la wake word — esattamente ciò che riceve model.
        predict(). Permette di verificare a orecchio (o inviando
        il file per un'analisi) se un tentativo fallito era dovuto
        a un audio già di per sé poco chiaro/distorto, o se
        l'audio era pulito e il modello semplicemente non l'ha
        riconosciuto.

        La copia dei dati avviene qui nel thread audio (veloce,
        solo numpy), la scrittura su disco (più lenta, I/O SD
        card) in un thread separato per non introdurre latenza nel
        loop audio principale.
        """

        try:
            buffered = np.concatenate(
                list(self.wake_debug_audio)
            ).astype(np.int16)
        except Exception as e:
            print(f"[WAKE-DEBUG] Errore bufferizzazione audio debug: {e}")
            return

        def _write():

            if not self.wake_debug_audio_write_lock.acquire(
                blocking=False
            ):
                return

            try:

                with wave.open(
                    WAKE_DEBUG_AUDIO_PATH, "wb"
                ) as wf:

                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(TARGET_SAMPLE_RATE)
                    wf.writeframes(buffered.tobytes())

            except Exception as e:

                print(
                    "[WAKE-DEBUG] Errore scrittura audio "
                    f"debug: {e}"
                )

            finally:

                self.wake_debug_audio_write_lock.release()

        threading.Thread(
            target=_write,
            daemon=True
        ).start()

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

        # Flag "speech" letto da DOA_VALUE (vedi XVF3800Telemetry.
        # hw_speech_active): un segnale indipendente dall'energia
        # per beam gia' usata sopra per energy_valid/direction_
        # active. Se il device/firmware non lo espone, e' sempre
        # True (fail-open), quindi non cambia nulla rispetto a
        # prima su un setup dove DOA_VALUE non e' disponibile.
        hw_speech_ok = (
            not WAKE_HW_SPEECH_GATE_ENABLED
            or self.xvf.hw_speech_active()
        )

        if (
            energy_valid
            and direction_active
            and hw_speech_ok
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

            if (
                energy_valid
                and direction_active
                and not hw_speech_ok
                and debug_config.DEBUG_LOGGING
            ):

                print(
                    "[SPEECH] Energia e direzione OK ma il chip "
                    "non rileva voce (DOA_VALUE speech=0): "
                    "ignorato (probabile musica/rumore dalla "
                    "stessa direzione)"
                )

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
        # DEBUG (solo se debug_config.DEBUG_LOGGING attivo)
        # ----------------------------------------------------

        if debug_config.DEBUG_LOGGING:

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

    def trim_leading_noise(self, mono):
        """
        Rimuove l'eventuale rumore/silenzio iniziale dal buffer
        assemblato (pre-roll + comando) prima di inviarlo a
        Vosk. Il pre-roll esiste per non perdere l'attacco della
        voce, ma può contenere un breve rumore ambientale (TV,
        click, coda del beep): la grammatica chiusa di Vosk non
        ha un "cestino" per il rumore ed è costretta a
        interpretarlo come la parola nota più vicina, anteponendola
        al comando vero (es. "alarm accendi luce tavolo" invece
        di "accendi luce tavolo").

        Scorre il buffer a blocchi di ~30ms e trova il primo
        blocco con energia sopra la soglia usata per il rilevamento
        voce dal vivo (stesso criterio di SPEECH_START_RATIO),
        poi taglia da lì con un piccolo margine di sicurezza.
        """

        block = int(0.03 * TARGET_SAMPLE_RATE)

        if len(mono) <= block:
            return mono

        margin = int(0.10 * TARGET_SAMPLE_RATE)

        threshold = (
            self.speech_gate.noise_floor
            * SPEECH_START_RATIO
        )

        mono_f = mono.astype(np.float32)

        for start in range(0, len(mono) - block, block):

            chunk = mono_f[start:start + block]

            rms = float(
                np.sqrt(np.mean(chunk * chunk))
            )

            if rms >= threshold:

                trim_at = max(0, start - margin)

                if trim_at > 0 and debug_config.DEBUG_LOGGING:

                    print(
                        "[REC] Tagliato rumore iniziale: "
                        f"{trim_at / TARGET_SAMPLE_RATE:.2f}s"
                    )

                return mono[trim_at:]

        # Nessun blocco energico trovato: lascia il buffer
        # invariato, meglio non rischiare di tagliare voce vera.
        return mono

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

            mono = self.trim_leading_noise(
                mono
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

        self.command_finished_at = (
            time.monotonic()
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
                    f"Engine      : vosk"
                )
                print(
                    f"Wake phrase : {self.vosk_wake_phrase}"
                )
                print(
                    f"Vosk model  : {VOSK_MODEL_PATH}"
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
                    f"HW speech   : "
                    f"{'on (DOA_VALUE, fail-open se assente)' if WAKE_HW_SPEECH_GATE_ENABLED else 'off'}"
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

                if self.ready_event is not None:
                    self.ready_event.set()

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

                            # A differenza di un modello wake-word
                            # dedicato, qui non serve azzerare
                            # nessun buffer di accumulo: il
                            # recognizer Vosk è già stato
                            # resettato dentro check_wakeword() nel
                            # momento stesso in cui ha rilevato la
                            # frase, quindi riparte pulito quando
                            # check_wakeword() verrà richiamato al
                            # ritorno in LISTENING (senza cuciture
                            # tra audio "vecchio" e "nuovo" come
                            # capiterebbe con un modello che
                            # bufferizza a chunk fissi).
                            self.last_wake_score = 0.0
                            self.last_wake_text = ""

                            # Catturiamo subito la direzione da
                            # cui è arrivata la wake word stessa:
                            # il comando verrà accettato solo se
                            # proviene dalla stessa sorgente,
                            # senza dover ricostruire un nuovo
                            # lock da zero (che tagliava l'inizio
                            # della frase).
                            #
                            # Preferiamo il flag hardware DOA_VALUE
                            # (stesso segnale che pilota il LED del
                            # device) a recent_direction(): con due
                            # sorgenti sonore contemporanee (una
                            # continua, l'altra che pronuncia la
                            # wake word), recent_direction() sceglie
                            # la lettura più energica e finisce per
                            # agganciarsi alla sorgente continua se
                            # è più forte — anche se non è lei ad
                            # aver detto la wake word. Se il device/
                            # firmware non espone DOA_VALUE,
                            # recent_hw_speech_direction() ritorna
                            # sempre None e ricadiamo sul
                            # comportamento precedente (fail-open).
                            wake_angle = (
                                self.xvf.recent_hw_speech_direction(
                                    WAKE_DIRECTION_LOOKBACK_SECONDS
                                )
                            )

                            if wake_angle is None:

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