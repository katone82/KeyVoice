# xvf3800-tool

Standalone XVF3800 tool for Raspberry Pi, independent from KeyVoice.

Based directly on the official reSpeaker `python_control/xvf_host.py` interface. The official host exposes firmware information, DoA, speech-energy telemetry and writable DSP parameters; configuration can be saved to flash.

## Install

```bash
./install.sh
```

Then copy the official `python_control/xvf_host.py` to `vendor/xvf_host.py`.

## First tests

```bash
sudo .venv/bin/python xvf3800_tool.py status
sudo .venv/bin/python xvf3800_tool.py telemetry
sudo .venv/bin/python xvf3800_tool.py monitor
```

## Initial voice profile

Apply without persistence first:

```bash
sudo .venv/bin/python xvf3800_tool.py configure
```

If the tests are good, persist:

```bash
sudo .venv/bin/python xvf3800_tool.py configure --save
```

Profile: AGC ON, limiter ON, echo suppression ON, non-linear echo attenuation ON, additional attenuation during non-speech ON, microphone HPF 125 Hz.

Noise-suppression floors and echo-strength/double-talk parameters are deliberately not forced until we measure the real room. The official host map documents `PP_MIN_NS`, `PP_MIN_NN`, `PP_GAMMA_*` and `PP_DTSENSITIVE` and their trade-offs.

## USB audio

Use ALSA separately:

```bash
arecord -l
arecord --dump-hw-params -D hw:X,Y
```

The control interface and USB audio stream are separate; this tool focuses on DSP control/telemetry.
