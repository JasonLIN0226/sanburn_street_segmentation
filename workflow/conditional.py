"""Conditional visual review and lossless batch views over the original renderer."""
from pathlib import Path
import argparse,hashlib,json
import numpy as np
from PIL import Image
import core
ROOT=Path.cwd()
def load(p):return json.loads(Path(p).read_text())
def save(p,v):Path(p).write_text(json.dumps(v,indent=2)+'\n')
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def work():return ROOT/'run/maps'/load('config.json')['map']
def output(rev):return work()/'tools'/rev

def decide(old,new,tiles,first_passed,new_roads=False,force_full=False,margin=128):
    if old.shape!=new.shape:raise ValueError('mask dimensions changed')
    delta=old!=new;changed=int(delta.sum());h,w=delta.shape
    if not changed and first_passed and not new_roads and not force_full:return {'mode':'reuse','reason':'same mask; complete first QA explicitly accepted; no new road or critical issue','required_tiles':[],'changed_pixels':0,'changed_fraction':0}
    affected=[]
    for t in tiles:
        a,b,c,d=t['box']
        if delta[max(0,b-margin):min(h,d+margin),max(0,a-margin):min(w,c+margin)].any():affected.append(t['id'])
    full=force_full or new_roads or not changed or changed/delta.size>.005 or len(affected)/max(1,len(tiles))>=.60
    reason='new roads, unresolved major issue, unknown/large influence, or >0.5% image pixels / >=60% tiles' if full else 'localized pixel changes; overlap/context margin included'
    return {'mode':'full' if full else 'partial','reason':reason,'required_tiles':[t['id'] for t in tiles] if full else affected,'changed_pixels':changed,'changed_fraction':changed/delta.size}

def plan(rev):
    state=load(work()/'workflow.json');qa=load('qa_r1.json');oldrev=qa['revision'];oldmask=output(oldrev)/'mask.png';newmask=output(rev)/'mask.png'
    if qa['mask_sha256']!=sha(oldmask) or qa['source_sha256']!=sha('source.png'):raise ValueError('first QA must match original and reviewed candidate')
    if set(qa['tiles'])!={t['id'] for t in state['tiles']}:raise ValueError('first QA coverage incomplete')
    oldinv=load(oldrev+'.json');inv=load(rev+'.json')
    added={r['id'] for r in inv['roads']}-{r['id'] for r in oldinv['roads']}
    result=decide(np.asarray(Image.open(oldmask)),np.asarray(Image.open(newmask)),state['tiles'],bool(qa['passed']),bool(added),bool(qa.get('critical_unresolved',False) or inv.get('requires_full_review',False)))
    if result['mode']=='partial':
        flagged={tid for tid,v in qa['tiles'].items() if v.get('issues')}
        needed=set(result['required_tiles'])|flagged
        result['required_tiles']=[t['id'] for t in state['tiles'] if t['id'] in needed]
        if len(needed)/len(state['tiles'])>=.60:
            result.update(mode='full',reason='changed and previously flagged tiles cover >=60% of map tiles',required_tiles=[t['id'] for t in state['tiles']])
    result.update({'selected_revision':rev,'first_review_revision':oldrev,'source_sha256':sha('source.png'),'mask_sha256':sha(newmask),'first_mask_sha256':sha(oldmask),'global_check_required':True,'global_check_scope':'source overview + selected mask overview; all four corners and all inventoried outward mouths; look for new/missed roads, not just changed pixels','threshold_notice':'0.5% pixel and 60% tile rules are predefined operational fallbacks, not validated accuracy thresholds.'})
    groups=[];by={t['id']:t for t in state['tiles']}
    for i in range(0,len(result['required_tiles']),2):
        ids=result['required_tiles'][i:i+2];groups.append({'tiles':ids,'images':[str(p) for tid in ids for p in (work()/'source_tiles'/(tid+'.png'),output(rev)/(tid+'.jpg'))],'boxes':{tid:by[tid]['box'] for tid in ids}})
    result['review_batches']=groups;save('review_plan.json',result);print(json.dumps({**result,'plan_sha256':sha('review_plan.json')}))

def views(request):
    req=load(request);rev=req.get('revision');out=ROOT/'detail_batches'/Path(request).stem;out.mkdir(parents=True,exist_ok=True)
    source=Image.open('source.png').convert('RGB');a=[]
    overlay=Image.open(output(rev)/'overlay.jpg') if rev else None
    for i,item in enumerate(req['regions']):
        box=item['box'];x0,y0,x1,y1=box
        if not(0<=x0<x1<=source.width and 0<=y0<y1<=source.height):raise ValueError('invalid native box')
        p=out/f'{i}_source.png';source.crop(tuple(box)).save(p);paths=[str(p)]
        if overlay:
            p=out/f'{i}_overlay.png';overlay.crop(tuple(box)).save(p);paths.append(str(p))
        a.append({'id':item.get('id',str(i)),'native_box':box,'images':paths})
    manifest={'regions':a,'note':'Native pixels, no resizing by this tool; batch at most four image blocks per viewing call.'};save(out/'manifest.json',manifest);print(json.dumps(manifest))

def finish(rev):
    p=load('review_plan.json');q=load('qa_final.json');cfg=load('config.json');inv=load(rev+'.json')
    if p['selected_revision']!=rev or q['selected_revision']!=rev or p['mask_sha256']!=sha(output(rev)/'mask.png') or q['mask_sha256']!=p['mask_sha256']:raise ValueError('stale final review / plan')
    if q['plan_sha256']!=sha('review_plan.json') or q['source_sha256']!=sha('source.png'):raise ValueError('review identity mismatch')
    if not set(p['required_tiles']).issubset(q['reviewed_tiles']):raise ValueError('required final tiles not reviewed')
    ids={t['id'] for t in load(work()/'workflow.json')['tiles']}
    if not set(q['reviewed_tiles']).issubset(ids) or any(not v.get('observation') for v in q['reviewed_tiles'].values()):raise ValueError('invalid tile review evidence')
    if not q.get('global_observation') or set(q['corners'])!={'northwest','northeast','southwest','southeast'}:raise ValueError('global/corner review required')
    expected={(r['id'],str(e.get('id',i))) for r in inv['roads'] for i,e in enumerate(r.get('exits',[]))}
    got={(e.get('road_id'),str(e.get('exit_id'))) for e in q['exit_checklist'] if e.get('evidence') and e.get('status')}
    if not expected.issubset(got):raise ValueError('missing exit conclusions')
    core.mark('export')
    for f in ('mask.png','overlay.jpg','overview.jpg'):(ROOT/f).write_bytes((output(rev)/f).read_bytes())
    summary={'map':cfg['map'],'selected_revision':rev,'review_mode':p['mode'],'review_reason':p['reason'],'source_tiles':len(inv['tiles']),'first_qa_tiles':len(load('qa_r1.json')['tiles']),'final_review_tiles':len(q['reviewed_tiles']),'inherited_qa_tiles':len(inv['tiles'])-len(q['reviewed_tiles']),'global_check':q['global_observation'],'exits':q['exit_checklist'],'unresolved':q['unresolved'],'mask_sha256':p['mask_sha256'],'notice':'Review coverage/identity validated; independent street accuracy is not established.'}
    save('summary.json',summary);core.mark('complete');print('Done: outputs and review decision recorded.')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['mark','build','plan','views','finish']);p.add_argument('items',nargs='*');a=p.parse_args()
    if a.action=='mark':core.mark(a.items[0])
    elif a.action=='build':
        core.build_all([a.items[0]+'.json']);print(json.dumps({'revision':a.items[0],'mask_sha256':sha(output(a.items[0])/'mask.png'),'source_sha256':sha('source.png')}))
    elif a.action=='plan':plan(a.items[0])
    elif a.action=='views':views(a.items[0])
    else:finish(a.items[0])
