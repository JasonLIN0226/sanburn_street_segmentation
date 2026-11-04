"""Remove obsolete UI-only records and recreatable previews; never remove runs."""
import re,time
from pathlib import Path
from common import read_json,load_rows,process_alive,recovered_job

def cleanup_stale(state,runs):
    state=Path(state);runs=Path(runs);jobs={};removed={'records':0,'previews':0,'bytes':0}
    for p in (state/'jobs').glob('*.json'):
        job=read_json(p)
        if not isinstance(job,dict) or not re.fullmatch(r'[0-9a-f]{16}',str(job.get('id',''))):continue
        jid=job['id'];jobs[jid]=job;name=job.get('case_name','')
        registry=read_json(state/'processes'/(jid+'.json')) or {}
        if not isinstance(name,str) or Path(name).name!=name or not name:continue
        active=job.get('status') in ('queued','preparing','running') or process_alive(registry.get('pid'))
        if active or (runs/name).is_dir() or process_alive(job.get('worker_pid')):continue
        for folder,suffix in [('jobs','.json'),('cancelled','.json'),('processes','.json'),('logs','.log')]:
            q=state/folder/(jid+suffix)
            try:
                if q.is_file():q.unlink();removed['records']+=1
            except OSError:pass
        jobs.pop(jid,None)
    recovered={}
    if runs.is_dir():
        for case in runs.iterdir():
            if not case.is_dir() or case.is_symlink():continue
            item=recovered_job(case)
            if item:recovered[item['id']]=case
    receipts={}
    for p in (state/'previews').glob('*'):
        m=re.fullmatch(r'([0-9a-f]{16})_(.*)',p.name)
        if not m or not p.is_file():continue
        jid,rest=m.groups();job=jobs.get(jid);registry=read_json(state/'processes'/(jid+'.json')) or {}
        if (job and job.get('status') in ('queued','preparing','running')) or process_alive(registry.get('pid')):continue
        try:
            if time.time()-p.stat().st_mtime<60:continue
        except OSError:continue
        case=runs/job['case_name'] if job and Path(str(job.get('case_name',''))).name==job.get('case_name') else recovered.get(jid)
        obsolete=not case or not case.is_dir()
        match=re.match(r'receipt_(\d+)_(\d+)_',rest)
        if not obsolete and match and job and job.get('status')=='completed':
            if jid not in receipts:
                rows=load_rows(case/'image_receipts.jsonl');snaps=load_rows(case/'image_snapshots.jsonl')
                pairs={(s.get('source_sha256'),tuple(s.get('display_size') or ())) for b in snaps for s in b.get('images',[]) if (case/s.get('path','')).is_file()}
                receipts[jid]=(rows,pairs)
            rows,pairs=receipts[jid];ri,ii=map(int,match.groups())
            if ri<len(rows) and ii<len(rows[ri].get('images',[])):
                item=rows[ri]['images'][ii];obsolete=(item.get('file_sha256'),tuple(item.get('display_size') or ())) in pairs
        if obsolete:
            try:size=p.stat().st_size;p.unlink();removed['previews']+=1;removed['bytes']+=size
            except OSError:pass
    return removed
