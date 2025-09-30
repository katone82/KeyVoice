#!/usr/bin/env python3
import json
import os

CONFIG_FILE = os.path.join(os.path.dirname(__file__), 'config.json')
SERVICE_FILE = '/etc/systemd/system/keyvoice.service'

# Chiavi richieste nel config
REQUIRED_KEYS = ['project_path', 'venv_path', 'service_user']

def main():
    with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
        config = json.load(f)
    missing = [k for k in REQUIRED_KEYS if k not in config]
    if missing:
        print(f"Errore: mancano le chiavi nel config: {missing}")
        return
    project_path = config['project_path']
    venv_path = config['venv_path']
    user = config['service_user']
    service = f'''[Unit]
Description=KeyVoice Home Automation Service
After=network.target

[Service]
Type=simple
WorkingDirectory={project_path}
ExecStart={venv_path}/bin/python {project_path}/run_service.py
Restart=on-failure
User={user}

[Install]
WantedBy=multi-user.target
'''
    print(f"Scrivo file di servizio in: {SERVICE_FILE}")
    with open('keyvoice.service', 'w', encoding='utf-8') as f:
        f.write(service)
    print("File keyvoice.service creato. Copialo con sudo in /etc/systemd/system e abilita con:\n")
    print("sudo cp keyvoice.service /etc/systemd/system/")
    print("sudo systemctl daemon-reload")
    print("sudo systemctl enable keyvoice")
    print("sudo systemctl start keyvoice")

if __name__ == '__main__':
    main()
