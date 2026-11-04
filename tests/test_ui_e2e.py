"""End-to-end UI process test with a fake local runner and zero API calls."""
import json,os,shutil,subprocess,sys,tempfile,time,unittest,urllib.request
from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
FAKE='''import argparse,json,shutil,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('action');p.add_argument('--map',required=True);p.add_argument('--run-id',required=True);p.add_argument('--source');a=p.parse_args()
root=Path(__file__).parent;case=root/'runs'/(a.map+'_api_'+a.run_id)
if a.action=='setup':
 case.mkdir(parents=True);shutil.copyfile(a.source,case/'source.png');(case/'config.json').write_text('{}');(case/'setup.json').write_text('{"seconds":0.1}')
else:
 time.sleep(1);shutil.copyfile(case/'source.png',case/'overlay.jpg');shutil.copyfile(case/'source.png',case/'mask.png');(case/'responses').mkdir()
 (case/'responses/0001.json').write_text('{"service_tier":"default"}')
 row={'seq':1,'model':'gpt-6.1-sol','usage':{'input_tokens':10000,'output_tokens':1000,'input_tokens_details':{'cached_tokens':2000,'cache_write_tokens':3000}}}
 (case/'requests.jsonl').write_text(json.dumps(row)+'\\n');(case/'summary.json').write_text('{}');(case/'execution.json').write_text('{"status":"completed","seconds":1}')
'''
class FullUiTest(unittest.TestCase):
 def test_detached_worker_preview_and_usage(self):
  with tempfile.TemporaryDirectory() as d:
   parent=Path(d);app=parent/'street';(app/'maps/city').mkdir(parents=True);Image.new('RGB',(30,20),'white').save(app/'maps/city/one.png');shutil.copytree(ROOT/'ui',app/'ui',ignore=shutil.ignore_patterns('__pycache__'));(app/'runner.py').write_text(FAKE)
   (parent/'.venv/bin').mkdir(parents=True);os.symlink(sys.executable,parent/'.venv/bin/python')
   proc=None
   def start():
    p=subprocess.Popen([sys.executable,str(app/'ui/server.py'),'--port','0','--no-open'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True);line=p.stdout.readline().strip();self.assertIn('http://127.0.0.1:',line);return p,line.split()[-1].rstrip('/')
   def stop(p):
    if p.poll() is None:p.terminate();p.wait(timeout=5)
    if p.stdout:p.stdout.close()
    if p.stderr:p.stderr.close()
   try:
    proc,base=start()
    with urllib.request.urlopen(base+'/api/session') as r:token=json.load(r)['token']
    req=urllib.request.Request(base+'/api/start',data=json.dumps({'maps':['city/one.png'],'workers':1}).encode(),headers={'Content-Type':'application/json','X-UI-Token':token})
    with urllib.request.urlopen(req) as r:jid=json.load(r)['job_ids'][0]
    stop(proc);proc,base=start()
    deadline=time.time()+20;job=None
    while time.time()<deadline:
     with urllib.request.urlopen(base+'/api/jobs') as r:jobs=json.load(r)['jobs']
     job=next((x for x in jobs if x['id']==jid),None)
     if job and job['status'] in ('completed','failed','interrupted'):break
     time.sleep(.15)
    self.assertEqual(job['status'],'completed',job);self.assertEqual(job['usage']['tokens'],11000);self.assertAlmostEqual(job['usage']['cost_usd'],.0277)
    for kind in ('source','overlay','mask'):
     with urllib.request.urlopen(base+f'/api/preview/{jid}/{kind}') as r:self.assertEqual(r.status,200);self.assertGreater(len(r.read()),40)
    with urllib.request.urlopen(base+f'/api/download/{jid}/mask') as r:self.assertEqual(r.status,200);self.assertIn('attachment',r.headers['Content-Disposition'])
   finally:
    if proc:stop(proc)
if __name__=='__main__':unittest.main()
