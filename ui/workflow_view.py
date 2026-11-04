"""Read-only reconstruction of the actual recorded workflow, on demand."""
import hashlib,json,re
from functools import lru_cache
from pathlib import Path
from common import load_rows,read_json

REVIEW_FILES=re.compile(r'^(draft_batch_\d+|inventory_r\d+|qa_r\d+|qa_final|qa_notes(?:_r\d+)?_batch_?\d+|revision_review|review_plan|detail_observations_\d+|summary|coverage_audit)\.json$')

def revisions(case):
    case=Path(case).resolve();cfg=read_json(case/'config.json') or {};name=cfg.get('map','')
    if not re.fullmatch(r'[A-Za-z0-9_-]+',str(name)):return []
    directory=case/'run/maps'/name/'tools';found=[]
    if not directory.is_dir():return found
    for p in sorted(directory.iterdir()):
        if re.fullmatch(r'inventory_r[0-9]+',p.name) and p.resolve().is_relative_to(case) and (p/'overview.jpg').is_file() and (p/'overview.jpg').resolve().is_relative_to(case):
            stat=(p/'overview.jpg').stat()
            found.append({'revision':p.name,'overview':str((p/'overview.jpg').relative_to(case)),'version':f'{stat.st_mtime_ns}-{stat.st_size}'})
    return found

def workflow_version(case):
    """Cheap file metadata fingerprint; avoids reading images or model output on each poll."""
    case=Path(case);files=[case/name for name in ('requests.jsonl','stages.jsonl','tool_calls.jsonl','image_receipts.jsonl','image_snapshots.jsonl','artifact_events.jsonl','rate_limit_events.jsonl','failure.json','execution.json')]
    try:files.extend(p for p in case.iterdir() if REVIEW_FILES.fullmatch(p.name))
    except OSError:pass
    for rev in revisions(case):files.append(case/rev['overview'])
    records=[]
    for p in sorted(files):
        try:
            stat=p.stat();records.append((p.name,stat.st_mtime_ns,stat.st_size))
        except OSError:pass
    return hashlib.sha256(json.dumps(records).encode()).hexdigest()

def workflow(case):
    case=Path(case);requests=load_rows(case/'requests.jsonl');receipts=load_rows(case/'image_receipts.jsonl');commands=load_rows(case/'tool_calls.jsonl')
    stages=load_rows(case/'stages.jsonl');events=[];ri=0;ci=0;captured={}
    for batch in load_rows(case/'artifact_events.jsonl'):
        key=(batch.get('seq'),batch.get('call_id'))
        for item in batch.get('artifacts',[]):
            sha=item.get('sha256');path=(case/item.get('path','')).resolve()
            if not re.fullmatch(r'[0-9a-f]{64}',str(sha)) or not path.is_relative_to((case/'agent_artifacts').resolve()):continue
            try:
                data=path.read_bytes()
                if len(data)>5_000_000 or hashlib.sha256(data).hexdigest()!=sha:continue
                kind=item.get('format','json')
                if kind not in ('json','text'):continue
                value=json.loads(data) if kind=='json' else {'observation':data.decode('utf-8')}
                captured.setdefault(key,[]).append({'type':'saved_artifact','name':item.get('name'),'data':value,'stage_after':batch.get('stage_after')})
            except (OSError,ValueError,UnicodeDecodeError):continue
    for row in requests:
        seq=row.get('seq')
        if not isinstance(seq,int) or seq<1:continue
        response=read_json(case/'responses'/f'{seq:04d}.json') or {};outputs=[]
        for item in response.get('output',[]):
            if item.get('type')=='message':
                for content in item.get('content',[]):
                    if content.get('type')=='output_text' and content.get('text'):outputs.append({'type':'text','text':content['text']})
            if item.get('type')!='function_call':continue
            name=item.get('name');args={}
            try:args=json.loads(item.get('arguments','{}'))
            except (ValueError,TypeError):pass
            if name=='view_images':
                paths=args.get('paths',[]);receipt=None
                if ri<len(receipts):
                    candidate=receipts[ri];images=candidate.get('images',[])
                    def canonical(raw):
                        p=Path(raw);return (p if p.is_absolute() else case/p).resolve()
                    try:
                        if [canonical(p) for p in paths]==[canonical(x['path']) for x in images]:receipt=candidate
                    except (TypeError,KeyError,ValueError):pass
                if receipt is not None:
                    outputs.append({'type':'images','images':[{'receipt':ri,'index':i,'path':x.get('path'),'native_size':x.get('native_size'),'display_size':x.get('display_size'),'version':x.get('file_sha256')} for i,x in enumerate(receipt['images'])]});ri+=1
                else:outputs.append({'type':'missing_images','paths':paths})
            elif name=='run_command':
                command=args.get('command','');match=None
                for k in range(ci,min(len(commands),ci+2)):
                    if commands[k].get('command')==command:match=commands[k];ci=k+1;break
                result=(match or {}).get('result') or {}
                outputs.append({'type':'command','command':command,'matched':match is not None,'exit_code':result.get('exit_code'),'stdout':result.get('stdout',''),'stderr':result.get('stderr',''),'seconds':(match or {}).get('seconds')})
                outputs.extend(captured.get((seq,item.get('call_id')),[]))
        usage=row.get('usage') or {}
        events.append({'seq':seq,'stage':row.get('stage'),'seconds':row.get('seconds'),'input_tokens':usage.get('input_tokens'),'output_tokens':usage.get('output_tokens'),'outputs':outputs})
    artifacts=[]
    if case.is_dir():
        for p in sorted(case.iterdir()):
            if p.is_file() and REVIEW_FILES.fullmatch(p.name):
                value=read_json(p)
                if value is not None:artifacts.append({'name':p.name,'data':value})
    return {'stages':stages,'events':events,'artifacts':artifacts,'revisions':revisions(case),'unmatched_image_calls':len(receipts)-ri,'note':'Agent observations are saved outputs, not ground truth. Stage previews show saved label versions.'}

@lru_cache(maxsize=512)
def file_hash(path,size,mtime,ctime):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for chunk in iter(lambda:f.read(1048576),b''):h.update(chunk)
    st=Path(path).stat()
    if (st.st_size,st.st_mtime_ns,st.st_ctime_ns)!=(size,mtime,ctime):return None
    return h.hexdigest()

def matches_hash(path,expected):
    try:
        st=path.stat();return file_hash(str(path),st.st_size,st.st_mtime_ns,st.st_ctime_ns)==expected
    except OSError:return False

def receipt_image(case,receipt,index):
    case=Path(case).resolve();rows=load_rows(case/'image_receipts.jsonl')
    if receipt<0 or receipt>=len(rows):return None
    images=rows[receipt].get('images',[])
    if index<0 or index>=len(images):return None
    item=images[index]
    # New runs preserve the exact PNG sent to the model, even if the source is later overwritten.
    for batch in load_rows(case/'image_snapshots.jsonl'):
        for snap in batch.get('images',[]):
            if snap.get('source_sha256')!=item.get('file_sha256') or snap.get('display_size')!=item.get('display_size'):continue
            path=(case/snap.get('path','')).resolve();expected=snap.get('snapshot_sha256')
            if not path.is_relative_to(case/'image_snapshots') or not path.is_file() or not re.fullmatch(r'[0-9a-f]{64}',str(expected)):continue
            if matches_hash(path,expected):return path
    p=(case/item.get('path','')).resolve()
    if not p.is_relative_to(case) or not p.is_file():return None
    if not matches_hash(p,item.get('file_sha256')):return None
    return p
