"""Read-only status of an existing combined PPT finalizer; no app control or replay."""
import argparse,base64,json
from pathlib import Path

def status(job):
    job=Path(job);result=job/'fast-finish-result.json'
    acceptance=job/'fast-accept-result.json'
    return {'id':job.name,'claimed':(job/'fast-finish.claim').exists(),'result':json.loads(result.read_text())if result.is_file()else None,
        'accept_claimed':(job/'fast-accept.claim').exists(),'accept_result':json.loads(acceptance.read_text())if acceptance.is_file()else None,
        'finish_helper_state':helper_state(job/'fast-finish.claim'),'accept_helper_state':helper_state(job/'fast-accept.claim'),'read_only':True}

def helper_state(claim):
    if not claim.exists():return 'not_claimed'
    import win32api,win32process,pywintypes
    try:
        value=json.loads(claim.read_text())
        if value.get('cancelled_before_dispatch')is True:return 'cancelled_before_dispatch'
        pid=value['pid'];h=win32api.OpenProcess(0x1000,False,pid)
        try:return 'alive'if win32process.GetExitCodeProcess(h)==259 else'exited'
        finally:h.Close()
    except pywintypes.error as error:return 'exited'if error.winerror==87 else'unknown'
    except (KeyError,ValueError,OSError):return 'unknown'

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('status',));parser.add_argument('job');args=parser.parse_args();job=Path(base64.urlsafe_b64decode(args.job[4:]).decode()if args.job.startswith('b64.')else args.job);print(json.dumps(status(job)))

