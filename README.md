# KeyVoice

**KeyVoice** è un'applicazione Python per il riconoscimento vocale e l'elaborazione di comandi vocali. Utilizza il motore di riconoscimento vocale **Vosk** e il sistema di wake word **Porcupine** per attivare l'ascolto in background. È ideale per progetti di domotica, assistenti vocali o interfacce vocali personalizzate.

---

## 📖 Indice

1. [Descrizione del progetto](#descrizione-del-progetto)  
2. [Workflow del progetto](#workflow-del-progetto)  
3. [Requisiti](#requisiti)  
4. [Pre-configurazione](#pre-configurazione)  
5. [Installazione](#installazione)  
6. [Struttura del progetto](#struttura-del-progetto)  
7. [Avvio del servizio](#avvio-del-servizio)  
8. [Riconoscimento vocale](#riconoscimento-vocale)  
9. [Elaborazione del comando](#elaborazione-del-comando)  
10. [Esecuzione dell’azione](#esecuzione-dellazione)  
11. [Feedback vocale](#feedback-vocale)  
12. [Debug e test](#debug-e-test)  
13. [Contatti](#contatti)  

---

## 1. Descrizione del progetto

KeyVoice permette di:

- Attendere un **wake word** (es. "jarvis") per attivare il riconoscimento vocale.  
- Trascrivere l’audio in tempo reale con **Vosk**.  
- Analizzare i comandi vocali e determinare le azioni da eseguire.  
- Eseguire azioni automatizzate in base ai comandi riconosciuti.  
- Fornire un **feedback vocale** all’utente sull’azione completata.

---

## 2. Workflow del progetto

Il flusso operativo generale:

1. Ascolto del wake word  
2. Riconoscimento vocale  
3. Elaborazione del comando  
4. Esecuzione dell’azione  
5. Feedback vocale  

---

## 3. Requisiti

- Python 3.6 o superiore  
- Microfono funzionante  
- Connessione Internet per configurazione iniziale del wake word (Porcupine)  

---

## 4. Pre-configurazione

Prima dell’installazione, crea o modifica `config.json` nella root del progetto:

```json
{
  "porcupine": {
    "access_key": "INSERISCI_TUA_API_KEY_PORCUPINE",
    "keywords": ["jarvis"],
    "sensitivity": 0.7,
    "pre_buffer_seconds": 0.5,
    "wakeword_trim_ms": 300,
    "silence_threshold": 500,
    "silence_duration": 2
  },
  "vosk": {
    "model_path": "models/vosk-model-small-it-0.22",
    "sample_rate": 16000
  }
}
```

---

## 5. Installazione

Clona il repository e installa le dipendenze:

```bash
sudo apt update
sudo apt install libportaudio2 libportaudiocpp0 portaudio19-dev

sudo apt update
sudo apt install python3-pip python3-venv python3-dev libasound2-dev portaudio19-dev

sudo apt update
sudo apt install python3.10 python3.10-venv python3.10-dev



git clone https://github.com/katone82/KeyVoice.git
cd KeyVoice
python3 -m venv keyvoiceenv
source keyvoiceenv/bin/activate
pip install --upgrade pip

pip uninstall pvporcupine
pip install pvporcupine==2.1.0  # The latest version often includes more ARM CPUs
pip install --upgrade pvporcupine

pip install -r requirements.txt
```

Scarica il modello Vosk per l’italiano da [qui](https://alphacephei.com/vosk/models) e inseriscilo nella cartella `models/`.

---

## 6. Struttura del progetto

```
KeyVoice/
├── src/
│   ├── keyvoice.py
│   ├── wake_word.py
│   ├── recognizer.py
│   ├── command_processor.py
│   ├── feedback.py
│   └── ...
├── models/
│   └── vosk-model-small-it-0.4/
├── config.json
├── requirements.txt
└── README.md
```

---

## 7. Avvio del servizio

Avvia KeyVoice con:

```bash
python3 src/keyvoice.py
```

---

### 7.1 Installazione del servizio

```bash
python3 create_services.py

#Scrivo file di servizio in: /etc/systemd/system/keyvoice.service
#File keyvoice.service creato. Copialo con sudo in /etc/systemd/system e abilita con:

sudo cp keyvoice.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable keyvoice
sudo systemctl start keyvoice

```
per leggere i log adesso occorre accedere a journalctl

```bash
sudo journalctl -u keyvoice -f
```

## 8. Riconoscimento vocale

Dopo il rilevamento del wake word, KeyVoice utilizza **Vosk** per trascrivere l’audio in testo.

---

## 9. Elaborazione del comando

Il testo trascritto viene analizzato da `command_processor.py`, che determina l’azione da eseguire in base ai comandi riconosciuti.

---

## 10. Esecuzione dell’azione

Le azioni (es. accendi una luce, avvia una musica) vengono eseguite tramite funzioni definite o integrate con sistemi di domotica.

---

## 11. Feedback vocale

KeyVoice fornisce un feedback vocale all’utente sull’esito dell’azione eseguita (ad esempio, “Luce accesa!”).

---

## 12. Debug e test

Per attivare la modalità debug, modifica il campo `debug` in `config.json`:

```json
{
  ...
  "debug": true
}
```

Utilizza log e messaggi di console per verificare il comportamento durante lo sviluppo.

---

## 13. Contatti

- **Autore:** [Luca Santarelli](https://github.com/katone82)
- **Email:** lucasantarelli82@gmail.com  
- **GitHub:** [KeyVoice Repo](https://github.com/katone82/KeyVoice)

---

**Licenza:** MIT  

pip install -r requirements.txt



