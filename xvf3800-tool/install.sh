#!/bin/bash
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
if [ ! -f vendor/xvf_host.py ]; then
 echo 'Copy official python_control/xvf_host.py into vendor/xvf_host.py'
fi
