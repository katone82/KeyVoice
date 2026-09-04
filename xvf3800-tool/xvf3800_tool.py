#!/usr/bin/env python3
from pathlib import Path
import argparse, json, math, sys, time, subprocess, csv, datetime

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'vendor'))

try:
    import xvf_host
except Exception as e:
    print(f'[ERROR] vendor/xvf_host.py missing or invalid: {e}')
    sys.exit(2)


def dev():
    d = xvf_host.find()
    if not d:
        raise RuntimeError('XVF3800 not found (VID 0x2886 PID 0x001A)')
    return d


def read(name):
    d = dev()
    try:
        return d.read(name)
    finally:
        d.close()


def show(name):
    value = read(name)
    print(f'{name:32} {value}')
    return value


def status(_):
    names = [
        'VERSION', 'BLD_MSG', 'BLD_REPO_HASH', 'BOOT_STATUS',
        'AEC_NUM_MICS', 'AEC_NUM_FARENDS', 'AEC_MIC_ARRAY_TYPE', 'USB_BIT_DEPTH'
    ]
    for name in names:
        try:
            show(name)
        except Exception as e:
            print(f'{name:32} ERROR {e}')


def telemetry(_):
    az = show('AEC_AZIMUTH_VALUES')
    en = show('AEC_SPENERGY_VALUES')
    degrees = [round(math.degrees(x) % 360, 1) for x in az]
    print('DoA degrees:                   ', degrees)
    print('Speech energy:                 ', [round(x, 1) for x in en])
    print('Speech (>0):                   ', [x > 0 for x in en])
    if len(en) >= 4:
        dominant = max(range(len(en)), key=lambda i: en[i])
        print(f'Dominant beam:                  {dominant + 1} ({degrees[dominant]:.1f} deg)')
        if en[dominant] > 0:
            second = sorted(en, reverse=True)[1] if len(en) > 1 else 0
            print(f'Dominance ratio:                 {en[dominant] / max(second, 1):.2f}x')


def monitor(a):
    print('XVF3800 telemetry monitor - CTRL+C to stop')
    print('time;beam1_deg;beam2_deg;beam3_deg;beam4_deg;energy1;energy2;energy3;energy4;dominant;ratio')
    try:
        while True:
            az = read('AEC_AZIMUTH_VALUES')
            en = read('AEC_SPENERGY_VALUES')
            deg = [math.degrees(x) % 360 for x in az]
            dom = max(range(len(en)), key=lambda i: en[i])
            second = sorted(en, reverse=True)[1] if len(en) > 1 else 0
            ratio = en[dom] / max(second, 1)
            now = datetime.datetime.now().strftime('%H:%M:%S.%f')[:-3]
            print(f'{now};' + ';'.join(f'{x:.1f}' for x in deg) + ';' +
                  ';'.join(f'{x:.0f}' for x in en) + f';{dom + 1};{ratio:.2f}', flush=True)
            time.sleep(a.interval)
    except KeyboardInterrupt:
        print('\n[OK] monitor stopped')


def dump(a):
    out = ROOT / (a.output or 'config/current.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    result = {
        'timestamp': datetime.datetime.now().isoformat(),
        'parameters': {}
    }
    try:
        result['firmware'] = list(read('VERSION'))
    except Exception:
        pass
    try:
        result['build'] = read('BLD_MSG')
    except Exception:
        pass
    try:
        result['repo_hash'] = read('BLD_REPO_HASH')
    except Exception:
        pass

    ok = 0
    errors = 0
    for name, info in xvf_host.PARAMETERS.items():
        access = info[0] if isinstance(info, (list, tuple)) and info else None
        if access == 'wo':
            continue
        try:
            value = read(name)
            result['parameters'][name] = value
            print(f'{name:32} {value}')
            ok += 1
        except Exception as e:
            print(f'{name:32} ERROR {e}')
            errors += 1

    out.write_text(json.dumps(result, indent=2, default=list) + '\n')
    print(f'\n[OK] dump: {out}')
    print(f'[OK] read={ok} errors={errors}')


def backup(a):
    a.output = a.output or f'config/backup_{datetime.datetime.now():%Y%m%d_%H%M%S}.json'
    dump(a)


def _write_profile(profile, save=False):
    d = dev()
    changed = []
    try:
        for name, value in profile['parameters'].items():
            if name not in xvf_host.PARAMETERS:
                raise RuntimeError(f'Unknown parameter: {name}')
            info = xvf_host.PARAMETERS[name]
            access = info[0] if isinstance(info, (list, tuple)) and info else None
            if access not in ('rw', 'wo'):
                raise RuntimeError(f'{name} is not writable (access={access})')
            vals = value if isinstance(value, list) else [value]
            print(f'{name:32} <- {vals}')
            d.write(name, vals)
            changed.append(name)
            if access == 'rw':
                try:
                    rb = d.read(name)
                    print(f'{"  readback":32} {rb}')
                except Exception as e:
                    print(f'{"  readback":32} ERROR {e}')
        if save:
            d.write('SAVE_CONFIGURATION', [1])
            print('[OK] configuration saved to flash')
    finally:
        d.close()
    return changed


def configure(a):
    profile = json.loads((ROOT / 'config' / 'voice_profile.json').read_text())
    print('Profile:', profile.get('name', 'unnamed'))
    _write_profile(profile, save=a.save)


def save(_):
    d = dev()
    try:
        d.write('SAVE_CONFIGURATION', [1])
    finally:
        d.close()
    print('[OK] saved')


def audio(_):
    subprocess.run(['arecord', '-l'], check=False)


def record(a):
    out = Path(a.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        'arecord', '-q', '-D', a.device, '-f', 'S16_LE',
        '-r', str(a.rate), '-c', str(a.channels), '-d', str(a.seconds), str(out)
    ]
    print('Recording:', ' '.join(cmd))
    subprocess.run(cmd, check=True)
    print(f'[OK] {out}')


def test(a):
    outdir = ROOT / 'recordings' / datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    outdir.mkdir(parents=True, exist_ok=True)
    wav = outdir / 'xvf3800.wav'
    csvfile = outdir / 'telemetry.csv'
    print(f'[TEST] duration={a.seconds}s device={a.device} rate={a.rate} channels={a.channels}')
    print('[TEST] Speak normally from different directions during the recording.')

    arecord_cmd = [
        'arecord', '-q', '-D', a.device, '-f', 'S16_LE',
        '-r', str(a.rate), '-c', str(a.channels), '-d', str(a.seconds), str(wav)
    ]
    proc = subprocess.Popen(arecord_cmd)
    start = time.monotonic()
    rows = []
    try:
        while proc.poll() is None:
            try:
                az = read('AEC_AZIMUTH_VALUES')
                en = read('AEC_SPENERGY_VALUES')
                deg = [math.degrees(x) % 360 for x in az]
                dom = max(range(len(en)), key=lambda i: en[i])
                second = sorted(en, reverse=True)[1] if len(en) > 1 else 0
                rows.append([
                    time.time(), *deg, *en, dom + 1,
                    en[dom] / max(second, 1)
                ])
            except Exception as e:
                print('[TELEMETRY ERROR]', e)
            time.sleep(a.interval)
    finally:
        proc.wait()

    with csvfile.open('w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['epoch','beam1_deg','beam2_deg','beam3_deg','beam4_deg',
                    'energy1','energy2','energy3','energy4','dominant_beam','dominance_ratio'])
        w.writerows(rows)

    print(f'[OK] audio:     {wav}')
    print(f'[OK] telemetry: {csvfile}')
    print('[TEST] Now you have a real synchronized audio + DSP telemetry sample.')


def main():
    ap = argparse.ArgumentParser(description='ReSpeaker XVF3800 USB DSP test/configuration tool')
    sp = ap.add_subparsers(dest='cmd', required=True)
    sp.add_parser('status')
    sp.add_parser('telemetry')
    sp.add_parser('audio')
    sp.add_parser('save')

    m = sp.add_parser('monitor')
    m.add_argument('--interval', type=float, default=0.25)

    d = sp.add_parser('dump')
    d.add_argument('--output', default='config/current.json')

    b = sp.add_parser('backup')
    b.add_argument('--output')

    c = sp.add_parser('configure')
    c.add_argument('--save', action='store_true')

    r = sp.add_parser('record')
    r.add_argument('--device', default='hw:1,0')
    r.add_argument('--seconds', type=int, default=10)
    r.add_argument('--rate', type=int, default=16000)
    r.add_argument('--channels', type=int, default=2)
    r.add_argument('--output', default='recordings/test.wav')

    t = sp.add_parser('test', help='real audio + XVF3800 telemetry test')
    t.add_argument('--device', default='hw:1,0')
    t.add_argument('--seconds', type=int, default=20)
    t.add_argument('--rate', type=int, default=16000)
    t.add_argument('--channels', type=int, default=2)
    t.add_argument('--interval', type=float, default=0.10)

    a = ap.parse_args()
    funcs = {
        'status': status, 'telemetry': telemetry, 'monitor': monitor,
        'configure': configure, 'save': save, 'dump': dump,
        'backup': backup, 'audio': audio, 'record': record, 'test': test
    }
    funcs[a.cmd](a)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('[ERROR]', e)
        sys.exit(1)
