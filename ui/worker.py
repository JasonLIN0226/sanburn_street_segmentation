"""Run batches independently of the HTTP/UI process; never call a model itself."""
import concurrent.futures,os,subprocess,sys,time,traceback
from pathlib import Path
from common import ROOT,RUNS,STATE,atomic_json,read_json,job_guard,cancelled

PYTHON=ROOT.parent/'.venv/bin/python'
RUNNER=ROOT/'runner.py'

def update(path,**values):
    data=read_json(path) or {};data.update(values);atomic_json(path,data)

def run_job(path):
    job=read_json(path)
    if not job or cancelled(STATE,job['id']):return
    log=STATE/'logs'/(job['id']+'.log');log.parent.mkdir(parents=True,exist_ok=True)
    src=(ROOT/'maps'/job['map_path']).resolve();maproot=(ROOT/'maps').resolve()
    if not src.is_relative_to(maproot) or not src.is_file():
        update(path,status='failed',finished_at=time.time(),error='Source map is missing or outside the maps directory.');return
    args=[str(PYTHON),str(RUNNER)];common=['--map',job['map_id'],'--run-id',job['run_id']]
    def execute(action,extra,f):
        # Serialize start and cancellation, so a queued job cannot start after deletion.
        with job_guard(STATE,job['id']):
            if cancelled(STATE,job['id']):return None
            command=args+[action]+common+extra
            process=subprocess.Popen(command,cwd=ROOT,stdout=f,stderr=subprocess.STDOUT,start_new_session=True)
            atomic_json(STATE/'processes'/(job['id']+'.json'),{'pid':process.pid,'command':command,'worker_pid':os.getpid()})
        code=process.wait()
        with job_guard(STATE,job['id']):
            registry=STATE/'processes'/(job['id']+'.json');current=read_json(registry)
            if current and current.get('pid')==process.pid:registry.unlink(missing_ok=True)
            if cancelled(STATE,job['id']):return None
        return code
    update(path,status='preparing',started_at=time.time(),worker_pid=os.getpid())
    try:
        with log.open('w',buffering=1) as f:
            f.write('Preparing source image and workspace\n')
            code=execute('setup',['--source',str(src)],f)
            if code is None:return
            if code:
                update(path,status='failed',finished_at=time.time(),error='Preparation failed; check the log.',exit_code=code);return
            update(path,status='running',setup_finished_at=time.time())
            f.write('\nStarting API segmentation workflow\n')
            code=execute('run',[],f)
            if code is None:return
        case=RUNS/job['case_name'];execution=read_json(case/'execution.json')
        if code==0 and execution and execution.get('status')=='completed' and (case/'mask.png').is_file():
            update(path,status='completed',finished_at=time.time(),exit_code=0)
        else:
            fail=read_json(case/'failure.json') or {}
            update(path,status='failed',finished_at=time.time(),error=fail.get('error','Workflow incomplete; check the log.'),exit_code=code)
    except Exception as e:
        with log.open('a') as f:f.write('\nScheduler error: '+type(e).__name__+'\n')
        update(path,status='failed',finished_at=time.time(),error='Scheduler failed: '+type(e).__name__)

def run_batch(batch_id):
    batch_file=STATE/'batches'/(batch_id+'.json');batch=read_json(batch_file)
    if not batch:raise ValueError('batch not found')
    paths=[STATE/'jobs'/(x+'.json') for x in batch['job_ids']]
    for p in paths:update(p,worker_pid=os.getpid())
    update(batch_file,status='running',worker_pid=os.getpid(),started_at=time.time())
    with concurrent.futures.ThreadPoolExecutor(max_workers=batch['workers']) as pool:
        futures=[pool.submit(run_job,p) for p in paths]
        for f in concurrent.futures.as_completed(futures):
            try:f.result()
            except Exception:traceback.print_exc()
    update(batch_file,status='finished',finished_at=time.time())

if __name__=='__main__':
    if len(sys.argv)!=2 or not sys.argv[1].isalnum():raise SystemExit('invalid batch id')
    run_batch(sys.argv[1])
