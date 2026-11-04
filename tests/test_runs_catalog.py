"""Run discovery, safe cleanup, and explicit permanent deletion."""
import json,os,sys,tempfile,time,unittest
from pathlib import Path
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'ui'))
import common,server,cleanup

class RunCatalogTest(unittest.TestCase):
 def test_discovery_missing_records_hidden_and_live_queue(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);runs=root/'runs';state=root/'state';runs.mkdir()
   case=runs/'MAP_api_cli';case.mkdir();(case/'config.json').write_text(json.dumps({'map':'MAP'}));(case/'execution.json').write_text('{"status":"completed","seconds":5}');(case/'mask.png').write_bytes(b'mask')
   stale='a'*16;live='b'*16
   common.atomic_json(state/'jobs'/(stale+'.json'),{'id':stale,'case_name':'missing','status':'completed'})
   common.atomic_json(state/'jobs'/(live+'.json'),{'id':live,'case_name':'pending','status':'queued','created_at':time.time()})
   with patch.multiple(common,RUNS=runs,STATE=state):
    jobs=common.jobs_list();self.assertEqual({x['case_name'] for x in jobs},{case.name,'pending'})
    found=next(x for x in jobs if x['case_name']==case.name);self.assertEqual(found['status'],'completed');self.assertEqual(found['elapsed_seconds'],5)
    common.atomic_json(state/'cancelled'/(found['id']+'.json'),{'preserved_case':str(case)})
    self.assertEqual([x['case_name'] for x in common.jobs_list()],['pending'])
    with patch.multiple(server,RUNS=runs,STATE=state):self.assertEqual(server.get_job(found['id'])[1],case.resolve())

 def test_cleanup_preserves_active_and_existing_runs(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);runs=root/'runs';state=root/'state';runs.mkdir();stale='a'*16;active='b'*16;done='c'*16
   for jid,name,status in [(stale,'missing','completed'),(active,'pending','running'),(done,'saved','completed')]:
    common.atomic_json(state/'jobs'/(jid+'.json'),{'id':jid,'case_name':name,'status':status})
   case=runs/'saved';case.mkdir();(case/'config.json').write_text('{}');(case/'image_snapshots').mkdir();(case/'image_snapshots/x.png').write_bytes(b'png')
   item={'file_sha256':'abc','display_size':[2,2]};(case/'image_receipts.jsonl').write_text(json.dumps({'images':[item]})+'\n')
   (case/'image_snapshots.jsonl').write_text(json.dumps({'images':[{'source_sha256':'abc','display_size':[2,2],'path':'image_snapshots/x.png'}]})+'\n')
   previews=state/'previews';previews.mkdir()
   paths=[previews/(x+'_receipt_0_0_1_2.png') for x in (stale,active,done)]
   for p in paths:p.write_bytes(b'preview');os.utime(p,(time.time()-100,time.time()-100))
   result=cleanup.cleanup_stale(state,runs)
   self.assertFalse((state/'jobs'/(stale+'.json')).exists());self.assertTrue((state/'jobs'/(active+'.json')).exists());self.assertTrue(case.exists())
   self.assertEqual([p.exists() for p in paths],[False,True,False]);self.assertEqual(result['previews'],2)

 def test_permanent_delete_is_scoped_and_refuses_active(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);runs=root/'runs';state=root/'state';jid='d'*16;case=runs/'case';case.mkdir(parents=True);(case/'mask.png').write_bytes(b'mask')
   common.atomic_json(state/'jobs'/(jid+'.json'),{'id':jid,'case_name':'case','status':'running'})
   with patch.multiple(server,RUNS=runs,STATE=state):
    with self.assertRaises(ValueError):server.remove_run(jid)
    self.assertTrue(case.exists());common.atomic_json(state/'jobs'/(jid+'.json'),{'id':jid,'case_name':'case','status':'completed'})
    self.assertFalse(server.remove_run(jid)['files_preserved']);self.assertFalse(case.exists())

if __name__=='__main__':unittest.main()
