"""Same labeling interface for fresh old/new agent runs."""
from pathlib import Path
import argparse, datetime, hashlib, json, pickle, sys, time
import numpy as np
from PIL import Image

ROOT=Path.cwd()
sys.path.insert(0,str(ROOT/'src'))
import regions, workflow as wf
regions.ROOT=ROOT/'run'

def mark(stage):
    row={'utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),'stage':stage}
    with (ROOT/'stages.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
    print(json.dumps(row))

class EdgeCache:
    def __init__(self,fn):self.fn=fn;self.cache={};self.images={};self.hits=0;self.misses=0
    def begin(self):self.images={}
    def __call__(self,g,a,b,r,w):
        if id(g) not in self.images:self.images[id(g)]=(g,hashlib.sha256(memoryview(g).cast('B')).hexdigest())
        key=(self.images[id(g)][1],g.shape,g.dtype.str,tuple(a),tuple(b),r,w)
        if key in self.cache:
            self.hits+=1;p,m=self.cache[key];return p.copy(),dict(m)
        self.misses+=1;p,m=self.fn(g,a,b,r,w);self.cache[key]=(p.copy(),dict(m));return p,m

def build_all(inventories):
    # One process retains the exact edge cache across revisions. Agent decisions
    # remain outside this function; no visual observation is generated here.
    cfg=json.loads((ROOT/'config.json').read_text()); name=cfg['map']; work=ROOT/'run/maps'/name
    orig=regions.snap_edge;cache=EdgeCache(orig)
    cachefile=ROOT/'edge_cache.pkl'
    version=hashlib.sha256(b''.join(p.read_bytes() for p in sorted((ROOT/'src').glob('*.py')))).hexdigest()
    if cfg['method']=='new':
        if cachefile.exists():
            saved=pickle.loads(cachefile.read_bytes())
            if saved['version']==version:cache.cache=saved['cache']
        regions.snap_edge=cache
    rows=[]
    for path in inventories:
        inv=json.loads(Path(path).read_text()); rev=Path(path).stem
        state=wf.load(work/'workflow.json');state['phase']='tools'
        if set(inv['tiles'])!={t['id'] for t in state['tiles']}:raise ValueError('missing source tile observations')
        for t in state['tiles']:t['source_status']='checked'
        # These are imported observations authored by the agent, not tool QA claims.
        state['roads']=inv['roads'];wf.save(work/'workflow.json',state)
        wf.save(ROOT/'run/reviews'/f'{name}_source.json',inv)
        cache.begin();h,m=cache.hits,cache.misses;start=time.perf_counter()
        regions.build(name,revision=rev)
        rows.append({'revision':rev,'seconds':time.perf_counter()-start,'hits':cache.hits-h,'misses':cache.misses-m})
    regions.snap_edge=orig
    if cfg['method']=='new':cachefile.write_bytes(pickle.dumps({'version':version,'cache':cache.cache}))
    with (ROOT/'tool_times.jsonl').open('a') as f:f.write(json.dumps({'builds':rows})+'\n')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['mark','build','finish']);p.add_argument('items',nargs='*');a=p.parse_args()
    if a.action=='mark':mark(a.items[0])
    elif a.action=='build':build_all(a.items)
    else:
        cfg=json.loads((ROOT/'config.json').read_text());name=cfg['map'];rev=a.items[0]
        target=ROOT/'run/maps'/name/'tools'/rev
        for f in ('mask.png','overlay.jpg','overview.jpg'):
            (ROOT/f).write_bytes((target/f).read_bytes())
        Image.open(ROOT/'mask.png').verify();mark('complete')
