# xvf3800-tool

Standalone test/configuration tool for ReSpeaker XVF3800 USB 4-Mic Array.

## Required vendor file

Copy the official `python_control/xvf_host.py` from:
`https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY`
into `vendor/xvf_host.py`.

## Commands

```bash
sudo .venv/bin/python xvf3800_tool.py status
sudo .venv/bin/python xvf3800_tool.py dump
sudo .venv/bin/python xvf3800_tool.py backup
sudo .venv/bin/python xvf3800_tool.py telemetry
sudo .venv/bin/python xvf3800_tool.py monitor
sudo .venv/bin/python xvf3800_tool.py audio
sudo .venv/bin/python xvf3800_tool.py configure
sudo .venv/bin/python xvf3800_tool.py configure --save
```

### Real test

First run `audio` and identify the XVF3800 ALSA device. Then:

```bash
sudo .venv/bin/python xvf3800_tool.py test --device hw:X,0 --seconds 20 --channels 2
```

The test creates a timestamped directory under `recordings/` containing:
- `xvf3800.wav` raw USB audio
- `telemetry.csv` synchronized DoA/speech-energy telemetry

Do not use `configure --save` until the initial profile has been tested.
