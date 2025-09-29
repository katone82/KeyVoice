import os
import json
import queue
import threading
import requests
import time

def invia_comando_ha(cmd, ha_url=None, ha_token=None):
    """
    Invia il comando a Home Assistant tramite REST API.
    """
    if ha_url is None or ha_token is None:
        with open("config.json", "r", encoding="utf-8") as f:
            CONFIG = json.load(f)
        ha_url = CONFIG['homeassistant']['url'].rstrip('/') + '/api/services'
        ha_token = CONFIG['homeassistant']['token']
    headers = {
        "Authorization": f"Bearer {ha_token}",
        "Content-Type": "application/json"
    }
    # Esempio: accendi/spegni switch/light
    if cmd['azione'] in ('accendi', 'spegni') and cmd['entity_id']:
        domain = cmd['entity_id'].split('.')[0]
        service = 'turn_on' if cmd['azione'] == 'accendi' else 'turn_off'
        url = f"{ha_url}/{domain}/{service}"
        data = {"entity_id": cmd['entity_id']}
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=5)
            if resp.ok:
                print(f"[HA] Comando inviato: {cmd['azione']} {cmd['entity_id']} -> OK")
            else:
                print(f"[HA] Errore risposta: {resp.status_code} {resp.text}")
        except Exception as e:
            print(f"[HA] Errore invio comando: {e}")
    else:
        print(f"[HA] Comando non gestito: {cmd}")

def ha_command_consumer(ha_url=None, ha_token=None):
    from fuzzy_parser import ha_command_queue, stop_event
    import time
    while not stop_event.is_set():
        try:
            cmd = ha_command_queue.get(timeout=1)
        except queue.Empty:
            continue
        invia_comando_ha(cmd, ha_url, ha_token)
        time.sleep(0.1)

if __name__ == "__main__":
    print("[HA] Thread consumer avviato. In attesa di comandi...")
    ha_command_consumer()
