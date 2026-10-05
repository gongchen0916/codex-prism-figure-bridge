"""Durable dispatch fence. A caller timeout is never a native cancellation."""
import datetime,hashlib,json,os,subprocess,tempfile,uuid
import re
from pathlib import Path

PENDING={'active','unknown','native_error'}
TERMINAL={'completed','completed_late','not_started','interrupted_process_exit'}

def signature(path):
    try:
        s=Path(path).stat();return [s.st_mtime_ns,s.st_size,s.st_ino]
    except FileNotFoundError:return None

def read(path):
    path=Path(path)
    if not path.exists():return None
    if path.is_symlink()or path.stat().st_size>32768:raise ValueError('Unsafe or oversized execution journal')
    value=json.loads(path.read_text())
    if not isinstance(value,dict)or value.get('state')not in PENDING|TERMINAL or value.get('schema')!=1:raise ValueError('Invalid execution journal')
    if not isinstance(value.get('operation_id'),str)or str(uuid.UUID(value['operation_id']))!=value['operation_id']:raise ValueError('Invalid operation identity')
    for field in('script','log','done','receipt'):
        if not isinstance(value.get(field),str)or not Path(value[field]).is_absolute():raise ValueError('Invalid operation path')
    if not isinstance(value.get('script_sha256'),str)or not re.fullmatch('[0-9a-f]{64}',value['script_sha256']):raise ValueError('Invalid script fingerprint')
    if not isinstance(value.get('before'),dict)or set(value['before'])!={'log','done','svg'}or not isinstance(value.get('prism_processes'),list):raise ValueError('Incomplete execution journal')
    if value.get('svg')is not None and(not isinstance(value['svg'],str)or not Path(value['svg']).is_absolute()):raise ValueError('Invalid SVG path')
    for sig in value['before'].values():
        if sig is not None and(not isinstance(sig,list)or len(sig)!=3 or any(type(v)is not int or v<0 for v in sig)):raise ValueError('Invalid artifact freshness signature')
    if any(not isinstance(p,dict)or type(p.get('pid'))is not int or p['pid']<=0 or not isinstance(p.get('started'),str)for p in value['prism_processes']):raise ValueError('Invalid observed process identity')
    return value

def atomic_write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink():raise ValueError('Execution journal may not be a symlink')
    fd,temp=tempfile.mkstemp(prefix='.operation-',dir=path.parent)
    try:
        with os.fdopen(fd,'w')as f:
            json.dump(value,f,ensure_ascii=False,allow_nan=False);f.flush();os.fsync(f.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)

def save(path,record,state,reason=''):
    value=dict(record,state=state,reason=reason,updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    atomic_write(path,value)
    # A completed stage can be removed by normal cleanup. Never recreate it
    # just to reconcile a late completion; its global receipt remains available.
    receipt=Path(value['receipt'])
    if receipt.parent.is_dir():atomic_write(receipt,value)
    return value

def begin(path,script,done,svg,processes):
    script=Path(script).resolve();op=str(uuid.uuid4());log=script.with_suffix('.log')
    value={'schema':1,'operation_id':op,'owner_pid':os.getpid(),'script':str(script),
        'script_sha256':hashlib.sha256(script.read_bytes()).hexdigest(),'log':str(log),'done':str(Path(done).resolve()),
        'svg':str(Path(svg).resolve())if svg is not None else None,'receipt':str(script.parent/('.prism-operation-'+op+'.json')),
        'before':{'log':signature(log),'done':signature(done),'svg':signature(svg)if svg is not None else None},
        'prism_processes':processes,'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    return save(path,value,'active')

def native_finished(record,read_log):
    try:
        if hashlib.sha256(Path(record['script']).read_bytes()).hexdigest()!=record['script_sha256']:return False
        for key in('log','done')+ (('svg',)if record.get('svg')else()):
            current=signature(record[key])
            if current is None or current==record['before'][key]:return False
        return 'COMPLETE! No Errors.'in read_log(Path(record['log']))and'PROBLEM! Not completed because of error.'not in read_log(Path(record['log']))
    except (OSError,KeyError,ValueError):return False

def process_inventory(app):
    """Read process identity only. Never activate, quit or manipulate an app."""
    executable=str(Path(app).resolve()/'Contents/MacOS/Prism')
    try:
        result=subprocess.check_output(['/bin/ps','-axo','pid=,lstart=,comm='],text=True,stderr=subprocess.DEVNULL,timeout=3,close_fds=True)
    except (OSError,subprocess.SubprocessError):return {'available':False,'instances':[]}
    rows=[]
    for line in result.splitlines():
        fields=line.strip().split(None,6)
        if len(fields)==7 and fields[0].isdigit()and fields[6].endswith('/Contents/MacOS/Prism'):rows.append({'pid':int(fields[0]),'started':' '.join(fields[1:6]),'executable':fields[6],'matches_target':fields[6]==executable})
    return {'available':True,'instances':rows}

def status(path,read_log):
    try:record=read(path)
    except (OSError,ValueError,KeyError,TypeError)as exc:return {'state':'invalid_journal','blocked':True,'error':str(exc),'result_verified':False}
    if record is None:return {'state':'idle','blocked':False,'operation_id':None,'result_verified':False}
    return {**record,'blocked':record['state']in PENDING,'late_completion_observed':record['state']in PENDING and native_finished(record,read_log),
            'native_completion_verified':record['state']in('completed','completed_late'),
            'result_verified':False,'note':'Native script completion is not figure/data acceptance; inspect the original operation outputs.'}
