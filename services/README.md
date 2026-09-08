
per il timer installare
pip install fastapi uvicorn 

per mqtt
python3 -m pip install paho-mqtt


curl http://localhost:8090/health

Creiamo un timer di prova
curl -X POST http://localhost:8090/timers \
-H "Content-Type: application/json" \
-d '{"duration":120,"name":"Pizza"}'