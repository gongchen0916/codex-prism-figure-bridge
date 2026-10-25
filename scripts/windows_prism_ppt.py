"""Create real Windows Prism OLE in a new PPT, not an image/hyperlink substitute."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import uuid
import math,re
from xml.etree import ElementTree as ET
import windows_prism_executor as executor
import execution_state


def _journal():
    import prism_bridge as bridge
    return bridge.PRISM_LOCK_PATH.with_name(bridge.PRISM_LOCK_PATH.name+'.ppt.json')

def _fast_finish_done(job,operation_id,source_sha256):
    """A closed parent does not release an in-flight combined finalizer."""
    path=job/'fast-finish-request.json'
    if not path.exists():return None
    def read(name):
        p=job/name
        if p.is_symlink()or p.stat().st_size>131072:raise ValueError('Unsafe combined PPT stage record')
        value=json.loads(p.read_text())
        if not isinstance(value,dict)or value.get('id')!=operation_id:raise ValueError('Combined PPT stage identity changed')
        return value
    intent=read('fast-finish-request.json')
    if (intent.get('schema')!=1 or intent.get('source_sha256')!=source_sha256
        or intent.get('ppt_request_sha256')!=hashlib.sha256((job/'ppt-request.json').read_bytes()).hexdigest()
        or intent.get('ppt_receipt_sha256')!=hashlib.sha256((job/'ppt-receipt.json').read_bytes()).hexdigest()):
        raise ValueError('Combined PPT intent differs from original native activation')
    names=('fast-finish.claim','fast-accept.claim','fast-finish-result.json','fast-accept-result.json','closed-native-acceptance.json')
    if not all((job/name).is_file()for name in names):return False
    for name in names[:2]:read(name)
    finish=read('fast-finish-result.json');accept=read('fast-accept-result.json');certificate=read('closed-native-acceptance.json')
    return (finish.get('status')=='completed'and accept.get('status')=='completed'and accept.get('acceptance')==certificate
        and certificate.get('fresh_readonly_reopen')is True and certificate.get('prior_actual_DoVerb_verified')is True
        and isinstance(accept.get('empty_office'),dict)
        and accept['empty_office'].get('status')in('empty_PowerPoint_normally_closed','existing_presentations_preserved','PowerPoint_already_exited'))


def execution_status():
    """Local receipt inspection only. Never starts or contacts either app."""
    path=_journal()
    if not path.exists():return {'state':'idle','blocked':False,'operation_id':None}
    try:
        if path.is_symlink()or path.stat().st_size>8192:raise ValueError('Unsafe PPT journal')
        value=json.loads(path.read_text())
        if value.get('schema')!=1 or str(uuid.UUID(value['id']))!=value['id']:raise ValueError('Invalid PPT operation identity')
        job=Path(value['job'])
        if not job.is_absolute()or job.name!=value['id']or value['state']not in('active','unknown','cancelled_before_dispatch','interrupted_process_exit'):raise ValueError('Invalid PPT operation journal')
        if value['state']in('cancelled_before_dispatch','interrupted_process_exit'):
            return {'state':value['state'],'blocked':False,'operation_id':value['id'],'native_success':False}
        request=json.loads((job/'ppt-request.json').read_text())
        if request.get('id')!=value['id']or request.get('source_sha256')!=value['source_sha256']:raise ValueError('PPT request identity changed')
        receipt=job/'ppt-receipt.json';complete=False;interactive=False
        if receipt.is_file():
            if receipt.is_symlink()or receipt.stat().st_size>32768:raise ValueError('Unsafe PPT receipt')
            result=json.loads(receipt.read_text())
            if result.get('id')!=value['id']or result.get('source_sha256')!=value['source_sha256']:raise ValueError('PPT completion identity differs')
            interactive=result.get('state')=='activated_awaiting_edit_verification'
            complete=(result.get('state')=='saved_reopened'and result.get('size_verified')is True
                and result.get('delivery_type')=='embedded_prism_ole'and result.get('scale')==1.0
                and result.get('click_action',{}).get('saved_reopened_verified')is True
                and all((job/name).is_file()and not(job/name).is_symlink()and(job/name).stat().st_size>0
                    for name in('prism_editable.pptx','slide-before.png','slide-reopened.png')))
            finished=job/'ppt-worker-finished.json'
            if request.get('activate')is True:complete=False
            elif complete:
                complete=False
                if finished.is_file()and not finished.is_symlink()and finished.stat().st_size<8192:
                    marker=json.loads(finished.read_text());complete=marker.get('id')==value['id']and marker.get('finished')is True
            closed=job/'ppt-session-closed.json'
            if closed.is_file():
                if closed.is_symlink()or closed.stat().st_size>8192:raise ValueError('Unsafe PPT session marker')
                marker=json.loads(closed.read_text());complete=marker.get('id')==value['id']and marker.get('closed')is True
        finish_done=_fast_finish_done(job,value['id'],value['source_sha256'])
        if finish_done is not None:complete=complete and finish_done
        return {'state':'completed'if complete else'interactive'if interactive else value['state'],
                'blocked':not complete,'operation_id':value['id'],'job':str(job),
                'combined_finish_pending':finish_done is False,
                'note':'PPT receipt completion is not scientific or figure-style approval.'}
    except (OSError,ValueError,KeyError,TypeError)as error:
        return {'state':'invalid_journal','blocked':True,'operation_id':None,'error':str(error)}


def recover(operation_id,policy):
    state=execution_status()
    if state.get('operation_id')!=operation_id:raise ValueError('PPT recovery operation identity differs')
    if not state['blocked']:return state
    value=json.loads(_journal().read_text());job=Path(value['job'])
    try:
        with (job/'ppt.claim').open('x')as stream:json.dump({'id':operation_id,'cancelled_before_dispatch':True},stream)
        value['state']='cancelled_before_dispatch'
    except FileExistsError:
        observed=executor.guest_call(policy,'ppt_status',job,timeout=10)
        if observed.get('id')!=operation_id or observed.get('available')is not True:
            raise RuntimeError('Cannot establish exact PPT helper/app state')
        if observed.get('helper_state')!='exited' or observed.get('instances'):
            raise RuntimeError('PPT helper or Prism/PowerPoint is still active; preserve unsaved work before recovery')
        if (job/'fast-finish-request.json').exists():
            helper=Path(__file__).with_name('windows_ppt_finalizer_status.py')
            finalizer=executor.guest_call(policy,'status',job,worker=helper,timeout=10)
            if (finalizer.get('id')!=operation_id or any(finalizer.get(key)not in('exited','not_claimed','cancelled_before_dispatch')
                for key in('finish_helper_state','accept_helper_state'))):
                raise RuntimeError('Combined PPT finalizer is alive or uncertain; no reset or replay')
            # An unclaimed late RPC cannot act after this terminal recovery.
            for name,key in(('fast-finish.claim','finish_helper_state'),('fast-accept.claim','accept_helper_state')):
                if finalizer[key]!='not_claimed':continue
                try:
                    with(job/name).open('x')as stream:json.dump({'id':operation_id,'cancelled_before_dispatch':True},stream)
                except FileExistsError:raise RuntimeError('Combined PPT stage was claimed during recovery; inspect again, no reset')
        value['state']='interrupted_process_exit'
    execution_state.atomic_write(_journal(),value)
    return execution_status()


def svg_physical_extent(svg):
    svg=Path(svg)
    if svg.is_symlink()or not svg.is_file():raise ValueError('Ordinary native SVG reference required')
    with svg.open('rb')as stream:
        prefix=stream.read(8192)
    if b'<!ENTITY'in prefix.upper():raise ValueError('SVG entity reference rejected')
    with svg.open('rb') as stream:
        root=next(ET.iterparse(stream,events=('start',)))[1]
    if root.tag!='{http://www.w3.org/2000/svg}svg':raise ValueError('Native SVG viewport required')
    values=[]
    for key in('width','height'):
        match=re.fullmatch(r'(\d+(?:\.\d+)?)pt',root.get(key,''))
        if not match:raise ValueError('Native SVG physical point dimensions required')
        value=float(match[1])
        if not math.isfinite(value)or not 0<value<=4000:raise ValueError('Native SVG extent exceeds PowerPoint delivery limit')
        values.append(value)
    if list(map(float,root.get('viewBox','').split()))!=[0,0,*values]:raise ValueError('Native SVG viewport is cropped/scaled')
    return {'width_pt':values[0],'height_pt':values[1]}

def embed(project,outdir,title='Prism figure',activate=False,policy=None,reference_svg=None):
    import prism_bridge as bridge
    with bridge.prism_export_lock(wait_timeout=30):
        bridge._check_execution_ready()
        return _embed_locked(project,outdir,title,activate,policy,reference_svg=reference_svg)


def _embed_locked(project,outdir,title,activate,policy,*,reference_svg=None):
    if policy is None:
        policy=executor.load_policy(Path(__file__).resolve().parents[1])
        if not policy['ready']:raise RuntimeError(policy['reason'])
    project=Path(project).resolve()
    if not project.is_file() or project.suffix.lower()not in('.prism','.pzfx','.pzf'):
        raise ValueError('Native Prism project required')
    physical=svg_physical_extent(reference_svg)if reference_svg is not None else None
    outdir=Path(outdir).resolve()
    outdir.mkdir(parents=True,exist_ok=False)
    ident=str(uuid.uuid4())
    job=Path(policy['shared_root'])/'PrismBridge'/'jobs'/ident
    (job/'input').mkdir(parents=True,exist_ok=False)
    name='figure'+project.suffix.lower()
    shutil.copyfile(project,job/'input'/name)
    request={'id':ident,'source':name,'source_sha256':hashlib.sha256(project.read_bytes()).hexdigest(),
             'deck':'prism_editable.pptx','title':title,'activate':activate}
    if physical is not None:
        request.update(reference_svg_sha256=hashlib.sha256(Path(reference_svg).read_bytes()).hexdigest(),
                       reference_extent=physical)
    (job/'ppt-request.json').write_text(json.dumps(request),encoding='utf-8')
    (outdir/'windows-ppt-job.json').write_text(json.dumps({'id':ident,'job':str(job)}),encoding='utf-8')
    record={'schema':1,'id':ident,'job':str(job),'source_sha256':request['source_sha256'],'state':'active'}
    execution_state.atomic_write(_journal(),record)
    try:result=executor.guest_call(policy,'embed',job,timeout=60,worker=Path(__file__).with_name('windows_prism_ppt_worker.py'))
    except BaseException:
        execution_state.atomic_write(_journal(),dict(record,state='unknown'))
        raise
    if result.get('id')!=ident:raise ValueError('PPT result identity mismatch')
    for path in job.iterdir():
        if path.suffix.lower()in('.pptx','.png'):shutil.copyfile(path,outdir/path.name)
    (outdir/'verification.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--project',required=True);parser.add_argument('--outdir',required=True)
    parser.add_argument('--title',default='Prism figure');parser.add_argument('--activate',action='store_true')
    args=parser.parse_args()
    print(json.dumps(embed(args.project,args.outdir,args.title,args.activate),ensure_ascii=False))


def package(project,pptx,title='Prism figure'):
    pptx=Path(pptx).resolve()
    if pptx.exists():raise ValueError('PPT destination already exists; original preserved')
    directory=pptx.parent/(pptx.stem+'-windows-ole')
    result=embed(project,directory,title,activate=False)
    shutil.copyfile(directory/'prism_editable.pptx',pptx)
    return result


if __name__=='__main__':main()
