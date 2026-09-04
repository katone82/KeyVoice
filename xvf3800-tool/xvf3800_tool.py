#!/usr/bin/env python3
from pathlib import Path
import argparse, json, math, sys, time
ROOT=Path(__file__).resolve().parent; sys.path.insert(0,str(ROOT/'vendor'))
try: import xvf_host
except Exception as e: print(f'[ERROR] vendor/xvf_host.py missing or invalid: {e}'); sys.exit(2)
def dev():
 d=xvf_host.find()
 if not d: raise RuntimeError('XVF3800 not found (VID 0x2886 PID 0x001A)')
 return d
def read(n):
 d=dev()
 try:return d.read(n)
 finally:d.close()
def show(n):
 v=read(n); print(f'{n:30} {v}'); return v
def status(_):
 for n in ['VERSION','BLD_MSG','BLD_REPO_HASH','BOOT_STATUS','AEC_NUM_MICS','AEC_NUM_FARENDS','AEC_MIC_ARRAY_TYPE','USB_BIT_DEPTH']: 
  try: show(n)
  except Exception as e: print(f'{n:30} ERROR {e}')
def telemetry(_):
 az=show('AEC_AZIMUTH_VALUES'); en=show('AEC_SPENERGY_VALUES')
 print('DoA degrees:',[round(math.degrees(x)%360,1) for x in az]); print('Speech:',[x>0 for x in en])
def monitor(a):
 try:
  while True:
   az=read('AEC_AZIMUTH_VALUES'); en=read('AEC_SPENERGY_VALUES')
   print('DoA=',[round(math.degrees(x)%360,1) for x in az],'speech=',[x>0 for x in en],'energy=',[round(x,5) for x in en]); time.sleep(a.interval)
 except KeyboardInterrupt: pass
def configure(a):
 p=json.loads((ROOT/'config/voice_profile.json').read_text()); d=dev()
 try:
  for n,v in p['parameters'].items():
   info=xvf_host.PARAMETERS[n]; vals=v if isinstance(v,list) else [v]; print(n,'<-',vals); d.write(n,vals)
  if a.save: d.write('SAVE_CONFIGURATION',[1]); print('[OK] saved')
 finally:d.close()
def save(_):
 d=dev()
 try:d.write('SAVE_CONFIGURATION',[1])
 finally:d.close()
 print('[OK] saved')
def main():
 ap=argparse.ArgumentParser(); sp=ap.add_subparsers(dest='cmd',required=True)
 for n in ['status','telemetry','save']:sp.add_parser(n)
 m=sp.add_parser('monitor');m.add_argument('--interval',type=float,default=.25)
 c=sp.add_parser('configure');c.add_argument('--save',action='store_true')
 a=ap.parse_args(); {'status':status,'telemetry':telemetry,'monitor':monitor,'configure':configure,'save':save}[a.cmd](a)
if __name__=='__main__':
 try:main()
 except Exception as e: print('[ERROR]',e);sys.exit(1)
