"""Offline integration check; synthetic data only, no API calls or old outputs."""
import importlib.util,json,tempfile,unittest
from pathlib import Path
from PIL import Image,ImageDraw
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('street_runner',ROOT/'runner.py');runner=importlib.util.module_from_spec(spec);spec.loader.exec_module(runner)

class IndependentWorkflowTest(unittest.TestCase):
 def test_setup_sandbox_image_and_renderer(self):
  with tempfile.TemporaryDirectory(prefix='street_test_',dir=ROOT) as d:
   root=Path(d);source=root/'source.png';im=Image.new('RGB',(256,256),'white');draw=ImageDraw.Draw(im);draw.line([(8,80),(248,80)],fill='black',width=2);draw.line([(8,170),(248,170)],fill='black',width=2);im.save(source)
   runner.MAP_NAME='TEST';runner.MAP_SOURCE=source;runner.CASE=root/'case';runner.setup();runner.sandbox_test()
   batches=json.loads((runner.CASE/'source_batches.json').read_text());self.assertEqual(sum(len(x['tiles']) for x in batches),1)
   tile=batches[0]['tiles'][0];out=runner.image_output([tile['image']]);self.assertEqual(out[1]['type'],'input_image')
   with self.assertRaises(ValueError):runner.image_output([str(ROOT/'workflow/conditional.py')])
   inv={'tiles':{tile['id']:{'roads':['synthetic'],'observation':'synthetic test','nonroad':'test','edge':'test'}},'roads':[{'id':'synthetic','name':'synthetic','polygon':[[10,80],[245,80],[245,170],[10,170]],'fixed_edges':[1,3],'radius':4,'exits':[],'obstacles':[]}],'exclusions':[],'unresolved':[]}
   (runner.CASE/'inventory_r1.json').write_text(json.dumps(inv));r=runner.run_command(str(runner.PYTHON)+' conditional.py build inventory_r1');self.assertEqual(r.get('exit_code'),0,r)
   mask=runner.CASE/'run/maps/TEST/tools/inventory_r1/mask.png';self.assertTrue(mask.exists())
   with Image.open(mask) as m:self.assertEqual(m.size,(256,256));self.assertEqual(m.getpixel((128,125)),255)
if __name__=='__main__':unittest.main()
