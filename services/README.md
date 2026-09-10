
per il timer installare
pip install fastapi uvicorn 

per mqtt
python3 -m pip install paho-mqtt


curl http://localhost:8090/health

Creiamo un timer di prova
curl -X POST http://localhost:8090/timers \
-H "Content-Type: application/json" \
-d '{"duration":120,"name":"Pizza"}'


# Integrazione con i comandi vocali

Il timer è raggiungibile anche a voce, tramite il fuzzy parser
(fuzzy_parser.py) che inoltra i comandi a questo servizio via
REST. La grammatica Vosk (vosk_listener.py) è chiusa, quindi
sono riconosciute solo le frasi generate da
build_timer_grammar(): prefisso + "di" + numero in parole +
unità (minuti 1-60, ore 1-12). Nessun nome/etichetta vocale
per ora, e un solo timer attivo alla volta.

Avvio (esempi):
  "timer di dieci minuti"
  "imposta un timer di dieci minuti"
  "avvia timer di due ore"

Cancellazione (esempi):
  "cancella il timer"
  "ferma timer"
  "annulla timer"
  "stop timer"

Configurazione opzionale in config.json (default già
localhost:8090, da impostare solo se il servizio gira altrove):

{
  "timer_service": {
    "url": "http://127.0.0.1:8090"
  }
}