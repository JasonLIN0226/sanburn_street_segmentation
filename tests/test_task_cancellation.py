"""Offline cancellation tests: each job has an isolated process group."""
import os,signal,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'ui'))
import common,server,worker

class CancellationTest(unittest.TestCase):
 def test_parallel_worker_cancels_one_and_finishes_the_other(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);state=root/'state';runs=root/'runs';(root/'maps').mkdir();(root/'maps/map.png').write_bytes(b'map')
   script=root/'fake_runner.py';script.write_text('''import argparse,json,time\nfrom pathlib import Path\np=argparse.ArgumentParser();p.add_argument("action");p.add_argument("--map");p.add_argument("--run-id");p.add_argument("--source");a=p.parse_args()\nr=Path(__file__).parent/"runs"/(a.map+"_api_"+a.run_id)\nif a.action=="setup":r.mkdir(parents=True)\nelse:\n time.sleep(1.5);(r/"mask.png").write_bytes(b"mask");(r/"execution.json").write_text(json.dumps({"status":"completed"}))\n''')
   ids=['1'*16,'2'*16];bid='b'*16
   for jid in ids:
    rid='ui_'+jid;common.atomic_json(state/'jobs'/(jid+'.json'),{'id':jid,'case_name':'MAP_api_'+rid,'map_id':'MAP','run_id':rid,'map_path':'map.png','status':'queued','cancel_safe':True})
   common.atomic_json(state/'batches'/(bid+'.json'),{'job_ids':ids,'workers':2})
   with patch.multiple(server,STATE=state,RUNS=runs),patch.multiple(worker,ROOT=root,RUNS=runs,STATE=state,RUNNER=script,PYTHON=Path(sys.executable)):
    thread=threading.Thread(target=worker.run_batch,args=(bid,));thread.start();deadline=time.time()+5
    while time.time()<deadline:
     entries=[common.read_json(state/'processes'/(jid+'.json')) for jid in ids]
     if all(x and 'run' in x['command'] for x in entries):break
     time.sleep(.02)
    else:self.fail('Both simulated jobs did not start')
    server.delete_job(ids[0]);thread.join(timeout=6);self.assertFalse(thread.is_alive())
    self.assertTrue(common.cancelled(state,ids[0]));self.assertEqual(common.read_json(state/'jobs'/(ids[1]+'.json'))['status'],'completed')
    self.assertTrue((runs/('MAP_api_ui_'+ids[1])/'mask.png').is_file())
 def test_stops_only_selected_job_and_preserves_case(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);state=root/'state';runs=root/'runs';script=root/'fake_runner.py'
   script.write_text('import time\ntime.sleep(60)\n')
   children=[]
   try:
    with patch.multiple(server,STATE=state,RUNS=runs),patch.multiple(common,STATE=state,RUNS=runs):
     for suffix in ('a','b'):
      jid=suffix*16;run='ui_'+jid;case=runs/('MAP_api_'+run);case.mkdir(parents=True);(case/'preserved.txt').write_text('saved')
      command=[sys.executable,str(script),'run','--map','MAP','--run-id',run]
      proc=subprocess.Popen(command,start_new_session=True);children.append(proc)
      common.atomic_json(state/'jobs'/(jid+'.json'),{'id':jid,'case_name':case.name,'map_id':'MAP','run_id':run,'status':'running','cancel_safe':True,'worker_pid':os.getpid()})
      common.atomic_json(state/'processes'/(jid+'.json'),{'pid':proc.pid,'command':command,'worker_pid':os.getpid()})
     result=server.delete_job('a'*16)
     self.assertTrue(result['files_preserved']);children[0].wait(timeout=5)
     self.assertIsNone(children[1].poll());self.assertTrue((runs/('MAP_api_ui_'+'a'*16)/'preserved.txt').is_file())
     self.assertEqual([x['id'] for x in common.jobs_list()],['b'*16])
   finally:
    for p in children:
     if p.poll() is None:os.killpg(p.pid,signal.SIGKILL)
     p.wait(timeout=5)
 def test_queued_cancelled_job_never_starts(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);state=root/'state';runs=root/'runs';jid='c'*16
   job={'id':jid,'case_name':'queued','map_id':'MAP','run_id':'queued','map_path':'map.png','status':'queued','cancel_safe':True}
   path=state/'jobs'/(jid+'.json');common.atomic_json(path,job)
   with patch.multiple(server,STATE=state,RUNS=runs),patch.multiple(worker,ROOT=root,RUNS=runs,STATE=state),patch.object(worker.subprocess,'Popen') as spawn:
    server.delete_job(jid);worker.run_job(path);spawn.assert_not_called()
 def test_unverified_process_is_not_signalled_or_deleted(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);state=root/'state';runs=root/'runs';jid='d'*16
   common.atomic_json(state/'jobs'/(jid+'.json'),{'id':jid,'case_name':'case','map_id':'MAP','run_id':'run','status':'running','cancel_safe':True})
   common.atomic_json(state/'processes'/(jid+'.json'),{'pid':os.getpid(),'command':['fake','fake','run']})
   with patch.multiple(server,STATE=state,RUNS=runs),patch.object(server.os,'killpg') as kill:
    with self.assertRaises(ValueError):server.delete_job(jid)
    kill.assert_not_called();self.assertFalse(common.cancelled(state,jid))
 def test_legacy_running_task_stops_without_stopping_its_sibling(self):
  with tempfile.TemporaryDirectory() as t:
   root=Path(t);state=root/'state';runs=root/'runs';script=root/'runner.py';script.write_text('import time\ntime.sleep(60)\n');children=[]
   try:
    for suffix in ('e','f'):
     jid=suffix*16;run='ui_'+jid
     p=subprocess.Popen([sys.executable,str(script),'run','--map','MAP','--run-id',run]);children.append(p)
     common.atomic_json(state/'jobs'/(jid+'.json'),{'id':jid,'case_name':run,'map_id':'MAP','run_id':run,'status':'running','worker_pid':os.getpid()})
    with patch.multiple(server,ROOT=root,STATE=state,RUNS=runs):server.delete_job('e'*16)
    children[0].wait(timeout=5);self.assertIsNone(children[1].poll())
   finally:
    for p in children:
     if p.poll() is None:p.kill()
     p.wait(timeout=5)
if __name__=='__main__':unittest.main()
