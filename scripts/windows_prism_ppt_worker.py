"""Native PowerPoint OLE acceptance/helper, preserving all unrelated documents."""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import uuid


def verify_size(shape, expected):
    actual={'width_pt':float(shape.Width),'height_pt':float(shape.Height)}
    if any(not math.isfinite(v) or v<=0 for v in (*actual.values(),*expected.values())):
        raise ValueError('Native OLE size must be finite and positive')
    if any(abs(actual[key]-expected[key])>0.01 for key in actual):
        raise ValueError('Reopened PPT native object size changed')
    return actual


def add_native_object(shapes, source, missing):
    # -1 requests the OLE server extent. This Windows COM signature rejects
    # omitted VT_ERROR values for the optional Single Width/Height arguments.
    # Never force a Prism graph into the former 520 x 365 pt rectangle.
    shape=shapes.AddOLEObject(48,104,-1,-1,'',str(source),False,'',0,'',False)
    verify_size(shape, {'width_pt':float(shape.Width),'height_pt':float(shape.Height)})
    shape.LockAspectRatio=-1
    return shape

def apply_reference_extent(shape,expected):
    server={'width_pt':float(shape.Width),'height_pt':float(shape.Height)}
    verify_size(shape,server)
    if set(expected)!={'width_pt','height_pt'}or any(not math.isfinite(v)or not 0<v<=4000 for v in expected.values()):
        raise ValueError('Bound native SVG physical extent required')
    # Reject a different selected graph/aspect, while permitting the observed
    # OLE extent rounding before correcting the copy-to-PPT scale.
    if abs((server['width_pt']/server['height_pt'])/(expected['width_pt']/expected['height_pt'])-1)>.002:
        raise ValueError('Prism OLE selected page differs from native SVG aspect')
    shape.LockAspectRatio=0
    shape.Width=expected['width_pt'];shape.Height=expected['height_pt']
    shape.LockAspectRatio=-1
    verify_size(shape,expected)
    return server


def configure_click(shape, verbs):
    edit=next((v for v in verbs if v.lower().startswith('edit') or v.startswith('编辑')),None)
    if edit is None:raise ValueError('Prism OLE exposes no recognized edit verb')
    action=shape.ActionSettings(1)  # ppMouseClick
    action.Action=11  # ppActionOLEVerb, not image/file hyperlink
    action.ActionVerb=edit
    return edit


def save_receipt(job, value):
    temp=job/'ppt-receipt.new'
    temp.write_text(json.dumps(value,ensure_ascii=True),encoding='utf-8')
    os.replace(temp,job/'ppt-receipt.json')


def embed(job):
    import pythoncom
    import win32com.client
    job=Path(job)
    request=json.loads((job/'ppt-request.json').read_text())
    if str(uuid.UUID(request['id']))!=job.name:
        raise ValueError('PPT operation identity mismatch')
    for name in (request['source'],request['deck']):
        if Path(name).name!=name or ':'in name:
            raise ValueError('Local filenames required')
    with (job/'ppt.claim').open('x')as f:f.write(str(os.getpid()))
    local=Path(os.environ['LOCALAPPDATA'])/'PrismBridge'/'ppt'/job.name
    local.mkdir(parents=True,exist_ok=False)
    source=job/'input'/request['source']
    if hashlib.sha256(source.read_bytes()).hexdigest()!=request['source_sha256']:
        raise ValueError('Prism input identity changed')
    shutil.copyfile(source,local/request['source'])
    pythoncom.CoInitialize()
    presentation=None
    try:
        ppt=win32com.client.Dispatch('PowerPoint.Application')
        ppt.Visible=-1
        presentation=ppt.Presentations.Add()
        presentation.PageSetup.SlideWidth=960
        presentation.PageSetup.SlideHeight=540
        slide=presentation.Slides.Add(1,12)
        title=slide.Shapes.AddTextbox(1,48,28,864,42)
        title.TextFrame.TextRange.Text=request['title']
        title.TextFrame.TextRange.Font.Size=22
        title.TextFrame.TextRange.Font.Name='Arial'
        shape=add_native_object(slide.Shapes,local/request['source'],pythoncom.Missing)
        native_size={'width_pt':float(shape.Width),'height_pt':float(shape.Height)}
        server_size=dict(native_size)
        if request.get('reference_extent')is not None:
            server_size=apply_reference_extent(shape,request['reference_extent'])
            native_size=dict(request['reference_extent'])
        # Enlarge this owned new slide if needed, never shrink the native graph.
        presentation.PageSetup.SlideWidth=max(960,native_size['width_pt']+96)
        presentation.PageSetup.SlideHeight=max(540,native_size['height_pt']+152)
        shape.Name='PrismBridge-'+request['id']
        shape.Tags.Add('PRISMBRIDGE_ID',request['id'])
        progid=shape.OLEFormat.ProgID
        if int(shape.Type)!=7 or 'prism'not in progid.lower():
            raise ValueError('PowerPoint did not create a native embedded Prism object')
        verbs=[str(shape.OLEFormat.ObjectVerbs.Item(i))for i in range(1,shape.OLEFormat.ObjectVerbs.Count+1)]
        edit_verb=configure_click(shape,verbs)
        deck=local/request['deck']
        presentation.SaveAs(str(deck),24)
        slide.Export(str(local/'slide-before.png'),'PNG',1600,900)
        presentation.Close();presentation=None
        presentation=ppt.Presentations.Open(str(deck),False,False,True)
        shape=presentation.Slides(1).Shapes('PrismBridge-'+request['id'])
        if shape.OLEFormat.ProgID!=progid or int(shape.Type)!=7:
            raise ValueError('Reopened PPT object identity changed')
        reopened_size=verify_size(shape,native_size)
        if shape.ActionSettings(1).Action!=11 or shape.ActionSettings(1).ActionVerb!=edit_verb:
            raise ValueError('Native Prism click action did not survive reopening')
        presentation.Slides(1).Export(str(local/'slide-reopened.png'),'PNG',1600,900)
        result={'id':request['id'],'state':'saved_reopened','progid':progid,'shape_name':shape.Name,
                'verbs':verbs,'windows_deck':str(deck),'source_sha256':request['source_sha256'],
                'size_policy':'native_ole_extent_1_to_1','native_size':native_size,
                'reopened_size':reopened_size,'size_verified':True,'scale':1.0,'delivery_type':'embedded_prism_ole',
                'click_action':{'action':11,'verb':edit_verb,'saved_reopened_verified':True}}
        result.update(ole_server_extent=server_size,
                      slide_extent={'width_pt':float(presentation.PageSetup.SlideWidth),'height_pt':float(presentation.PageSetup.SlideHeight)},
                      object_position={'left_pt':float(shape.Left),'top_pt':float(shape.Top)})
        if request.get('reference_extent')is not None:
            result.update(size_policy='native_svg_physical_extent_1_to_1',reference_svg_sha256=request['reference_svg_sha256'],
                          reference_extent=request['reference_extent'],native_svg_extent_verified=True,
                          ole_extent_adjustment={'x':native_size['width_pt']/server_size['width_pt'],'y':native_size['height_pt']/server_size['height_pt']})
        try:
            shape.Export(str(local/'native-object-reopened.png'),2)
            shutil.copyfile(local/'native-object-reopened.png',job/'native-object-reopened.png')
            result['native_object_preview_exported']=True
        except Exception as error:
            result.update(native_object_preview_exported=False,object_preview_error=str(error)[-200:])
        save_receipt(job,result)
        for name in (request['deck'],'slide-before.png','slide-reopened.png'):
            shutil.copyfile(local/name,job/name)
        if request.get('activate'):
            shape.OLEFormat.DoVerb()
            result['state']='activated_awaiting_edit_verification'
            save_receipt(job,result)
            # Leave only this owned test presentation and activated native Prism
            # object open for edit/save/update verification. Never call PPT.Quit.
            presentation=None
        else:
            presentation.Close();presentation=None
            marker=job/'ppt-worker-finished.new'
            marker.write_text(json.dumps({'id':request['id'],'finished':True}))
            os.replace(marker,job/'ppt-worker-finished.json')
        return result
    finally:
        if presentation is not None:presentation.Close()
        pythoncom.CoUninitialize()


def collect(job,reopen=False):
    """Save the exact existing test deck after user/native Prism edits."""
    import pythoncom,win32com.client
    job=Path(job)
    request=json.loads((job/'ppt-request.json').read_text())
    receipt=json.loads((job/'ppt-receipt.json').read_text())
    pythoncom.CoInitialize()
    try:
        ppt=win32com.client.GetActiveObject('PowerPoint.Application')
        matches=[p for p in ppt.Presentations if str(p.FullName).lower()==receipt['windows_deck'].lower()]
        if len(matches)!=1:raise ValueError('Exact owned test presentation is not open')
        presentation=matches[0]
        shape=presentation.Slides(1).Shapes(receipt['shape_name'])
        if shape.Tags.Item('PRISMBRIDGE_ID')!=request['id'] or 'prism'not in shape.OLEFormat.ProgID.lower():
            raise ValueError('PPT source ownership changed')
        presentation.Save()
        local=Path(receipt['windows_deck']).parent
        presentation.Slides(1).Export(str(local/'slide-after.png'),'PNG',1600,900)
        shutil.copyfile(local/request['deck'],job/request['deck'])
        shutil.copyfile(local/'slide-after.png',job/'slide-after.png')
        receipt['state']='updated_saved'
        if reopen:
            presentation.Close()
            presentation=ppt.Presentations.Open(receipt['windows_deck'],False,False,True)
            shape=presentation.Slides(1).Shapes(receipt['shape_name'])
            if int(shape.Type)!=7 or 'prism'not in shape.OLEFormat.ProgID.lower():
                raise ValueError('Updated OLE did not survive reopening')
            presentation.Slides(1).Export(str(local/'slide-final-reopened.png'),'PNG',1600,900)
            shutil.copyfile(local/'slide-final-reopened.png',job/'slide-final-reopened.png')
            shape.OLEFormat.DoVerb()
            receipt['state']='updated_reopened_activated'
        save_receipt(job,receipt)
        return receipt
    finally:pythoncom.CoUninitialize()


def inspect(job):
    import pythoncom,win32com.client
    job=Path(job);receipt=json.loads((job/'ppt-receipt.json').read_text())
    pythoncom.CoInitialize()
    try:
        ppt=win32com.client.GetActiveObject('PowerPoint.Application')
        p=next(p for p in ppt.Presentations if str(p.FullName).lower()==receipt['windows_deck'].lower())
        shape=p.Slides(1).Shapes(receipt['shape_name'])
        try:
            obj=shape.OLEFormat.Object
            typeinfo=obj._oleobj_.GetTypeInfo()
            attr=typeinfo.GetTypeAttr()
            methods=[typeinfo.GetNames(typeinfo.GetFuncDesc(i)[0])for i in range(attr[8])]
            return {'id':receipt['id'],'typeinfo':str(attr),'methods':methods}
        except Exception as exc:
            return {'id':receipt['id'],'automation_object_error':str(exc)}
    finally:pythoncom.CoUninitialize()


def close_owned(job):
    """Save/close only the exact test presentation retained by --activate."""
    import pythoncom,win32com.client
    job=Path(job);request=json.loads((job/'ppt-request.json').read_text())
    receipt=json.loads((job/'ppt-receipt.json').read_text())
    pythoncom.CoInitialize()
    try:
        ppt=win32com.client.GetActiveObject('PowerPoint.Application')
        matches=[p for p in ppt.Presentations if str(p.FullName).lower()==receipt['windows_deck'].lower()]
        if not matches:
            (job/'ppt-session-closed.json').write_text(json.dumps({'id':request['id'],'closed':True}))
            return {'id':request['id'],'state':'owned_deck_already_closed'}
        if len(matches)!=1:raise ValueError('Ambiguous owned presentation')
        p=matches[0];shape=p.Slides(1).Shapes(receipt['shape_name'])
        if shape.Tags.Item('PRISMBRIDGE_ID')!=request['id']:raise ValueError('Owned presentation identity changed')
        p.Save();p.Close()
        (job/'ppt-session-closed.json').write_text(json.dumps({'id':request['id'],'closed':True}))
        return {'id':request['id'],'state':'owned_deck_saved_closed'}
    finally:pythoncom.CoUninitialize()


def edit_test(job):
    """Acceptance only: run a documented script against the activated test object.

    No Open/Exit/Quit, no external documents; failure leaves the owned test state.
    """
    import pythoncom,win32com.client
    job=Path(job);request=json.loads((job/'ppt-request.json').read_text())
    receipt=json.loads((job/'ppt-receipt.json').read_text())
    if not request['title'].startswith('Synthetic data:'):
        raise ValueError('Embedded edit proof is limited to synthetic acceptance decks')
    with (job/'edit-v3.claim').open('x')as f:f.write(str(os.getpid()))
    local=Path(receipt['windows_deck']).parent
    script=local/'edit_test_v3.pzc'
    (local/'edit-values.csv').write_text('1.4\n1.2\n0.9\n1.1\n',encoding='utf-8')
    # An embedded object has no disk filename. PZC Save without a filename
    # fails; the containing PowerPoint presentation saves the OLE storage.
    script.write_text('CreateLog\nSetPath "'+str(local)+'"\nGoTo D, 1\nImport "edit-values.csv", 1, 1, 1\nGoTo G, 1\nSetGraphTitle "Embedded update verified"\nExportSVG "embedded-after.svg"\nOpenOutput "edit_v3_done.txt", CLEAR\nWText "done"\nCloseOutput\n',encoding='utf-8')
    pythoncom.CoInitialize()
    try:
        ppt=win32com.client.GetActiveObject('PowerPoint.Application')
        p=next(p for p in ppt.Presentations if str(p.FullName).lower()==receipt['windows_deck'].lower())
        shape=p.Slides(1).Shapes(receipt['shape_name'])
        if shape.Tags.Item('PRISMBRIDGE_ID')!=request['id']:raise ValueError('Wrong embedded source')
        shape.OLEFormat.DoVerb()
        command=win32com.client.Dispatch('Prism.Command')
        command.RunCommand(str(script))
        for path in (script.with_suffix('.log'),local/'edit_v3_done.txt',local/'embedded-after.svg'):
            if path.exists():shutil.copyfile(path,job/path.name)
        return {'id':receipt['id'],'script_completed':(local/'edit_v3_done.txt').is_file()}
    finally:pythoncom.CoUninitialize()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('embed','collect','finish','inspect','edit_test','close_owned'));parser.add_argument('job')
    args=parser.parse_args()
    if args.job.startswith('b64.'):
        args.job=base64.urlsafe_b64decode(args.job[4:]).decode('utf8')
    print(json.dumps({'embed':embed,'collect':collect,'finish':lambda j:collect(j,True),'inspect':inspect,'edit_test':edit_test,'close_owned':close_owned}[args.action](args.job),ensure_ascii=True))
