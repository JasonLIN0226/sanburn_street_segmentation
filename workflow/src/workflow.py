"""Agent-led source-only inventory -> bounded tools -> explicit QA.
No model is called here. Visual review is performed by the operating agent.
"""
import argparse,hashlib,json,os,re
from pathlib import Path
import numpy as np
from PIL import Image,ImageDraw
from corridor import propose,reconstruct,spec_hash,seam_alerts,extend_exit
from protection import propose_exclusion,apply_exclusion
from seam import transition
Image.MAX_IMAGE_PIXELS=None


def sha(path):
 h=hashlib.sha256()
 with open(path,'rb') as f:
  for b in iter(lambda:f.read(1<<20),b''):h.update(b)
 return h.hexdigest()

def save(path,obj):
 p=Path(path);tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2));os.replace(tmp,p)

def load(path):return json.loads(Path(path).read_text())

def safe_id(value):
 if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',value):raise ValueError('invalid task id')
 return value

def offsets(size,tile,overlap):
 if tile<=overlap or overlap<0:raise ValueError('invalid tile/overlap')
 if size<=tile:return [0]
 return sorted(set(list(range(0,size-tile+1,tile-overlap))+[size-tile]))

def init(work,source,baseline=None,tile=1536,overlap=256):
 work=Path(work);source=Path(source).resolve()
 if work.exists():raise FileExistsError('choose a new workspace')
 im=Image.open(source).convert('RGB');w,h=im.size;tiles=[]
 for y in offsets(h,tile,overlap):
  for x in offsets(w,tile,overlap):
   b=[x,y,min(w,x+tile),min(h,y+tile)];s=[k for k,v in [('west',x==0),('north',y==0),('east',b[2]==w),('south',b[3]==h)] if v]
   tiles.append({'id':f'x{x}_y{y}','box':b,'edge_sides':s,'source_status':'pending','qa_status':'pending'})
 tiles.sort(key=lambda t:(not bool(t['edge_sides']),t['id']))
 if baseline:
  m=np.asarray(Image.open(baseline).convert('L'))>0
  if m.shape!=(h,w):raise ValueError('baseline is not aligned')
 else:m=np.zeros((h,w),bool)
 work.mkdir(parents=True);(work/'source_tiles').mkdir();(work/'qa_tiles').mkdir();(work/'tools').mkdir()
 Image.fromarray(m.astype('uint8')*255).save(work/'candidate.png');Image.fromarray(np.zeros((h,w),np.uint8)).save(work/'state.png')
 state={'schema':1,'source':str(source),'source_sha256':sha(source),'size_wh':[w,h],'baseline':str(Path(baseline).resolve()) if baseline else None,'baseline_sha256':sha(baseline) if baseline else None,'phase':'source','tiles':tiles,'roads':[],'tasks':[],'unresolved':[],'candidate_sha256':sha(work/'candidate.png'),'note':'Agent visual receipts are claims, not independent ground truth.'};save(work/'workflow.json',state);return state

def verified(work):
 work=Path(work);s=load(work/'workflow.json')
 if sha(s['source'])!=s['source_sha256']:raise ValueError('source changed')
 if sha(work/'candidate.png')!=s['candidate_sha256']:raise ValueError('candidate changed outside workflow')
 return s

def make_crop(work,s,tile,qa=False):
 work=Path(work);im=Image.open(s['source']).convert('RGB').crop(tuple(tile['box']))
 if qa:
  x0,y0,x1,y1=tile['box'];mask=np.asarray(Image.open(work/'candidate.png').convert('L').crop((x0,y0,x1,y1)))>0;a=np.asarray(im).astype(float);a[mask]=a[mask]*.48+np.array([20,140,240])*.52;im=Image.fromarray(a.astype('uint8'))
 path=work/('qa_tiles' if qa else 'source_tiles')/(tile['id']+'.png');im.save(path);return {'crop':str(path),'crop_sha256':sha(path)}

def next_task(work):
 s=verified(work)
 if s['phase']=='source':
  tile=next((t for t in s['tiles'] if t['source_status']!='checked'),None)
  if tile:return {'stage':'source_only','tile':tile,**make_crop(work,s,tile),'instructions':'看原图，记录道路、非道路、障碍、每个纸边和角落；不要使用 mask。'}
  return {'stage':'reconcile_inventory','instructions':'合并跨图块道路，列明每个出口、障碍与仍未解决事项。'}
 if s['phase']=='tools':return {'stage':'tools','roads':s['roads'],'unresolved':s['unresolved'],'instructions':'按 road_id 生成候选并由视觉 agent 选择；未确认问题不得静默消除。'}
 if s['phase']=='qa':
  tile=next((t for t in s['tiles'] if t['qa_status']!='checked'),None)
  if tile:return {'stage':'mask_qa','tile':tile,**make_crop(work,s,tile,True),'source_crop':str(Path(work)/'source_tiles'/(tile['id']+'.png')),'candidate_sha256':s['candidate_sha256'],'instructions':'对照原图检查漏标、溢出、接缝和边缘；记录证据，不得默认通过。'}
  return {'stage':'review_unresolved','unresolved':s['unresolved']}
 return {'stage':s['phase']}

def record_source(work,tile_id,r):
 work=Path(work);s=verified(work)
 if s['phase']!='source':raise ValueError('not source phase')
 t=next(t for t in s['tiles'] if t['id']==tile_id)
 if t['source_status']=='checked':raise ValueError('tile already reviewed')
 path=work/'source_tiles'/(tile_id+'.png')
 if not path.exists() or r.get('crop_sha256')!=sha(path) or r.get('source_sha256')!=s['source_sha256'] or r.get('box')!=t['box']:raise ValueError('receipt does not match crop or source')
 if not r.get('observation') or not isinstance(r.get('candidates'),list):raise ValueError('source observation required')
 if not r['candidates'] and not r.get('no_road_evidence'):raise ValueError('empty list needs visual evidence')
 for side in t['edge_sides']:
  if not r.get('edge_evidence',{}).get(side):raise ValueError('each edge and corner needs evidence')
 for c in r['candidates']:
  if c.get('classification') not in ['road','nonroad','uncertain'] or not c.get('evidence') or not c.get('box'):raise ValueError('candidate lacks classification/location/evidence')
  x0,y0,x1,y1=c['box'];a,b,d,e=t['box']
  if not(a<=x0<x1<=d and b<=y0<y1<=e):raise ValueError('candidate outside tile')
  if c['classification']=='road' and (not c.get('id') or not c.get('continuation') or 'obstacles' not in c):raise ValueError('road needs id, continuation, obstacle inventory')
 t['source_status']='checked';t['source_receipt']=r;save(work/'workflow.json',s);return next_task(work)

def finish_source(work,inventory):
 work=Path(work);s=verified(work)
 if s['phase']!='source' or any(t['source_status']!='checked' for t in s['tiles']):raise ValueError('source tiles not all reviewed')
 if not isinstance(inventory.get('roads'),list) or not isinstance(inventory.get('unresolved'),list):raise ValueError('inventory and unresolved list required')
 ids=[]
 for r in inventory['roads']:
  if not all(r.get(k) for k in ['id','evidence','continuation']) or not isinstance(r.get('exits'),list) or not isinstance(r.get('obstacles'),list):raise ValueError('road inventory incomplete')
  if r['id'] in ids:raise ValueError('duplicate canonical road id')
  ids.append(r['id'])
 observed={c['id'] for t in s['tiles'] for c in t['source_receipt']['candidates'] if c['classification']=='road'}
 aliases=inventory.get('aliases',{})
 if any(aliases.get(i,i) not in ids for i in observed):raise ValueError('observed road missing from inventory')
 unresolved=list(inventory['unresolved'])
 for r in inventory['roads']:
  for i,exit_ in enumerate(r['exits']):
   if not isinstance(exit_,dict) or not exit_.get('status') or not exit_.get('evidence'):raise ValueError('each listed exit needs status and evidence')
   if exit_['status'] not in ('labelled','not_road','outside_policy'):unresolved.append({'type':'inventory_exit','road_id':r['id'],'exit_index':i,'evidence':exit_['evidence']})
 s.update({'phase':'tools','roads':inventory['roads'],'aliases':aliases,'unresolved':unresolved,'inventory_sha256':spec_hash(inventory)});save(work/'workflow.json',s);return next_task(work)

def create_proposal(work,road_id,request):
 work=Path(work);s=verified(work)
 if s['phase']!='tools' or road_id not in [r['id'] for r in s['roads']]:raise ValueError('finish source inventory and select known road first')
 box=request['box'];x0,y0,x1,y1=box;w,h=s['size_wh']
 if not(0<=x0<x1<=w and 0<=y0<y1<=h):raise ValueError('box outside source')
 name=safe_id(request['id']);out=work/'tools'/name
 if out.exists():raise ValueError('task already exists')
 im=Image.open(s['source']).convert('RGB').crop(tuple(box));gray=np.asarray(im.convert('L'));proposal,paths=propose(gray,request['spec']);proposal.update({'source_sha256':s['source_sha256'],'candidate_before_sha256':s['candidate_sha256'],'box':box,'road_id':road_id,'task_id':name});out.mkdir();im.save(out/'source.png');save(out/'request.json',request);save(out/'proposal.json',proposal);np.savez_compressed(out/'paths.npz',**paths)
 for side in ['low','high']:
  for item in proposal['candidates'][side]:
   q=im.copy();t=range(request['spec']['span'][0],request['spec']['span'][1]+1);pts=[(int(v),int(z)) if request['spec']['axis']=='y' else (int(z),int(v)) for z,v in zip(t,paths[item['id']])];ImageDraw.Draw(q).line(pts,fill='#00ffff',width=3);q.save(out/(item['id']+'.png'))
 return {'task':name,'source':str(out/'source.png'),'proposal':str(out/'proposal.json'),'proposal_sha256':spec_hash(proposal),'note':'视觉 agent 必须查看候选再选择；不自动选择得分最高的线。'}

def propose_object(work,task_id,request):
 work=Path(work);s=verified(work);out=work/'tools'/safe_id(task_id)
 if s['phase']!='tools':raise ValueError('not tool phase')
 task=load(out/'request.json');proposal=load(out/'proposal.json')
 if proposal['candidate_before_sha256']!=s['candidate_sha256'] or proposal['source_sha256']!=s['source_sha256']:raise ValueError('stale road proposal')
 name=safe_id(request['id']);target=out/('object_'+name)
 if target.exists():raise ValueError('object proposal already exists')
 rgb=np.asarray(Image.open(out/'source.png').convert('RGB'));negative,info=propose_exclusion(rgb,request['box'],request['seed'])
 target.mkdir();Image.fromarray(negative.astype('uint8')*255).save(target/'negative.png')
 a=rgb.astype(float);a[negative]=a[negative]*.4+np.array([255,40,30])*.6;Image.fromarray(a.astype('uint8')).save(target/'preview.png')
 meta={'id':name,'task_id':task_id,'candidate_before_sha256':s['candidate_sha256'],'source_sha256':s['source_sha256'],'negative_sha256':sha(target/'negative.png'),'preview_sha256':sha(target/'preview.png'),'info':info,'request':request};save(target/'proposal.json',meta)
 return {'proposal':str(target/'proposal.json'),'preview':str(target/'preview.png'),'proposal_sha256':spec_hash(meta),'preview_sha256':meta['preview_sha256'],'note':'先对照原图审查红色区域，再提交确认。'}

def apply_task(work,task_id,selection,objects):
 work=Path(work);s=verified(work)
 if s['phase']!='tools':raise ValueError('not tool phase')
 out=work/'tools'/task_id;request=load(out/'request.json');proposal=load(out/'proposal.json');paths=dict(np.load(out/'paths.npz'));box=request['box'];x0,y0,x1,y1=box
 if proposal['source_sha256']!=s['source_sha256'] or proposal['candidate_before_sha256']!=s['candidate_sha256']:raise ValueError('stale proposal')
 im=Image.open(s['source']).convert('RGB').crop(tuple(box));rgb=np.asarray(im);full=(np.asarray(Image.open(work/'candidate.png').convert('L'))>0).astype('uint8');before=full[y0:y1,x0:x1].copy();after,meta=reconstruct(before,request['spec'],proposal,paths,selection)
 if selection.get('seam_transition'):
  after,unc,seam=transition(before,after,request['spec'],paths,selection);meta['transition']=seam
 else:unc=np.zeros_like(before)
 applied=[]
 for obj in objects:
  target=out/('object_'+safe_id(obj['id']));info=load(target/'proposal.json')
  if info['candidate_before_sha256']!=s['candidate_sha256'] or info['source_sha256']!=s['source_sha256'] or obj.get('proposal_sha256')!=spec_hash(info) or obj.get('preview_sha256')!=sha(target/'preview.png') or info['negative_sha256']!=sha(target/'negative.png'):raise ValueError('object proposal or visual receipt stale')
  negative=np.asarray(Image.open(target/'negative.png').convert('L'))>0;after,op=apply_exclusion(after,negative,obj);unc[negative]=2;applied.append({'proposal':info,'decision':obj,'operation':op})
 meta['seam_alerts']=seam_alerts(before,after,request['spec']);meta.pop('alerts');full[y0:y1,x0:x1]=after
 state=np.asarray(Image.open(work/'state.png').convert('L')).copy();st=state[y0:y1,x0:x1];st[before!=after]=2;st[unc>0]=2
 Image.fromarray(full*255).save(work/'candidate.png');Image.fromarray(state).save(work/'state.png');s['candidate_sha256']=sha(work/'candidate.png');s['tasks'].append({'task_id':task_id,'road_id':proposal['road_id'],'box':box,'meta':meta,'objects':applied,'candidate_sha256':s['candidate_sha256']});
 if meta['seam_alerts']:s['unresolved'].append({'task':task_id,'type':'seam','details':meta['seam_alerts']})
 save(out/'selection.json',selection);save(out/'audit.json',s['tasks'][-1]);save(work/'workflow.json',s);return {'candidate':str(work/'candidate.png'),'status':'candidate_needs_visual_QA','meta':meta}

def extend_task(work,request):
 work=Path(work);s=verified(work)
 if s['phase']!='tools' or request.get('road_id') not in [r['id'] for r in s['roads']]:raise ValueError('select a known road in tool phase')
 name=safe_id(request['id']);out=work/'tools'/name
 if out.exists():raise ValueError('task already exists')
 if request.get('candidate_before_sha256')!=s['candidate_sha256'] or request.get('source_sha256')!=s['source_sha256']:raise ValueError('stale exit task')
 box=request['box'];x0,y0,x1,y1=box;w,h=s['size_wh']
 if not(0<=x0<x1<=w and 0<=y0<y1<=h):raise ValueError('box outside source')
 paper=Path(request['paper_path'])
 if sha(paper)!=request.get('paper_sha256'):raise ValueError('paper evidence changed')
 pg=(np.asarray(Image.open(paper).convert('L'))>0).astype('uint8')
 if pg.shape!=(y1-y0,x1-x0):raise ValueError('paper mask not aligned to crop')
 decision=request['decision']
 if decision.get('boundary_kind') not in ('paper','crop'):raise ValueError('state whether boundary is actual paper or crop')
 if decision['boundary_kind']=='paper' and not decision.get('paper_review_evidence'):raise ValueError('actual paper boundary needs visual review evidence')
 full=(np.asarray(Image.open(work/'candidate.png').convert('L'))>0).astype('uint8');before=full[y0:y1,x0:x1].copy();fixed,unc,meta=extend_exit(before,pg,request['axis'],request['anchor'],request['stop'],request['low'],request['high'],decision)
 full[y0:y1,x0:x1]=fixed;state=np.asarray(Image.open(work/'state.png').convert('L')).copy();st=state[y0:y1,x0:x1];st[unc>0]=2
 out.mkdir();save(out/'request.json',request);Image.fromarray(unc).save(out/'uncertainty.png');Image.fromarray(full*255).save(work/'candidate.png');Image.fromarray(state).save(work/'state.png');s['candidate_sha256']=sha(work/'candidate.png')
 s['tasks'].append({'task_id':name,'road_id':request['road_id'],'box':box,'kind':'exit_extension','meta':meta,'candidate_sha256':s['candidate_sha256']})
 if decision['boundary_kind']=='crop':s['unresolved'].append({'type':'crop_boundary','task':name,'evidence':'延伸仅至裁片边，原地图继续部分尚未核查。'})
 save(out/'audit.json',s['tasks'][-1]);save(work/'workflow.json',s);return {'candidate':str(work/'candidate.png'),'status':'inferred_needs_visual_QA','meta':meta}

def begin_qa(work):
 work=Path(work);s=verified(work)
 if s['phase']!='tools' or not s['tasks']:raise ValueError('no tool tasks')
 s['phase']='qa';s['qa_candidate_sha256']=s['candidate_sha256'];save(work/'workflow.json',s);return next_task(work)

def record_qa(work,tile_id,r):
 work=Path(work);s=verified(work)
 if s['phase']!='qa' or s['qa_candidate_sha256']!=s['candidate_sha256']:raise ValueError('candidate is stale')
 t=next(t for t in s['tiles'] if t['id']==tile_id);path=work/'qa_tiles'/(tile_id+'.png')
 if t['qa_status']=='checked' or not path.exists() or r.get('crop_sha256')!=sha(path) or r.get('candidate_sha256')!=s['candidate_sha256']:raise ValueError('wrong QA image')
 if not r.get('observation') or not isinstance(r.get('issues'),list):raise ValueError('QA evidence required')
 if not r['issues'] and not r.get('no_issue_evidence'):raise ValueError('clean tile needs concrete evidence')
 for side in t['edge_sides']:
  if not r.get('edge_evidence',{}).get(side):raise ValueError('each QA edge needs evidence')
 for issue in r['issues']:
  if issue.get('type') not in ('missing','overflow','seam','uncertain','other') or not issue.get('evidence'):raise ValueError('issue needs type and evidence')
  if s.get('strict_visual_gate') and (issue.get('severity') not in ('major','minor') or not issue.get('location')):raise ValueError('strict QA issues need severity and location')
 t['qa_status']='checked';t['qa_receipt']=r;s['unresolved']+=r['issues'];save(work/'workflow.json',s);return next_task(work)

def return_to_tools(work):
 work=Path(work);s=verified(work)
 if s['phase']!='qa':raise ValueError('not QA phase')
 s['phase']='tools'
 for t in s['tiles']:t['qa_status']='pending';t.pop('qa_receipt',None)
 save(work/'workflow.json',s);return next_task(work)

def resolve_issue(work,decision):
 work=Path(work);s=verified(work)
 if s['phase'] not in ('tools','qa'):raise ValueError('wrong phase')
 idx=decision.get('index');
 if not isinstance(idx,int) or not 0<=idx<len(s['unresolved']) or not decision.get('evidence') or decision.get('candidate_sha256')!=s['candidate_sha256']:raise ValueError('resolution needs current version and visual evidence')
 item=s['unresolved'][idx]
 if item.get('candidate_before_discovery')==s['candidate_sha256']:raise ValueError('newly discovered road requires a changed candidate before resolution')
 if item.get('type')=='inventory_exit':
  if decision.get('exit_status') not in ('labelled','not_road','outside_policy'):raise ValueError('exit resolution requires explicit status')
  road=next(r for r in s['roads'] if r['id']==item['road_id']);road['exits'][item['exit_index']]['status']=decision['exit_status'];road['exits'][item['exit_index']]['resolution_evidence']=decision['evidence']
 s['unresolved'].pop(idx);s.setdefault('resolution_history',[]).append({'issue':item,'decision':decision});save(work/'workflow.json',s);return status(work)

def record_discovered_exit(work,road_id,tile_id,evidence):
 """Record an exit newly discovered during feedback, without rewriting source receipts."""
 work=Path(work);s=verified(work)
 if s['phase']!='qa' or not evidence:raise ValueError('a visual QA observation is required')
 t=next(t for t in s['tiles'] if t['id']==tile_id)
 if t['qa_status']!='checked' or not any(x['type']=='missing' for x in t['qa_receipt']['issues']):raise ValueError('exit discovery needs a checked QA tile with a missing-road issue')
 road=next(r for r in s['roads'] if r['id']==road_id);i=len(road['exits'])
 road['exits'].append({'status':'pending','evidence':evidence,'discovered_during':'qa','qa_candidate_sha256':s['candidate_sha256'],'qa_tile':tile_id})
 s['unresolved'].append({'type':'inventory_exit','road_id':road_id,'exit_index':i,'evidence':evidence})
 s.setdefault('inventory_updates',[]).append({'road_id':road_id,'exit_index':i,'tile_id':tile_id,'candidate_sha256':s['candidate_sha256'],'evidence':evidence})
 save(work/'workflow.json',s)

def finalize(work,checklist):
 work=Path(work);s=verified(work)
 if s.get('strict_visual_gate'):
  import boundary_pairs
  review_path=work.parent.parent/'reviews'/f'{work.name}_review.json'
  if not review_path.exists():raise ValueError('strict visual review is missing')
  review=load(review_path)
  if review.get('candidate_sha256')!=s['candidate_sha256'] or boundary_pairs.review_gate(review)['status']!='reviewed':raise ValueError('strict visual review incomplete, unresolved, or stale')
 blocking=[i for i in s['unresolved'] if i.get('type')=='inventory_exit' or i.get('severity')!='minor'] if s.get('strict_visual_gate') else s['unresolved']
 if s['phase']!='qa' or any(t['qa_status']!='checked' for t in s['tiles']) or blocking:raise ValueError('QA incomplete or unresolved major issues')
 if any(t['source_status']!='checked' for t in s['tiles']):raise ValueError('source review incomplete')
 for t in s['tiles']:
  q=t['qa_receipt'];r=t['source_receipt']
  if q['candidate_sha256']!=s['candidate_sha256'] or q['crop_sha256']!=sha(work/'qa_tiles'/(t['id']+'.png')):raise ValueError('stale QA receipt')
  if r['source_sha256']!=s['source_sha256'] or r['crop_sha256']!=sha(work/'source_tiles'/(t['id']+'.png')):raise ValueError('stale source receipt')
 if any(e.get('status') not in ('labelled','not_road','outside_policy') for r in s['roads'] for e in r['exits']):raise ValueError('road exit remains unresolved')
 expected={r['id'] for r in s['roads']};received=checklist.get('roads',{})
 if set(received)!=expected or any(not (v.get('checked') is True and v.get('evidence')) for v in received.values()):raise ValueError('each road needs explicit completeness evidence')
 if not checklist.get('edge_evidence') or not checklist.get('limitations'):raise ValueError('edge evidence and limitations required')
 if s.get('qa_candidate_sha256')!=s['candidate_sha256']:raise ValueError('QA candidate stale')
 s['phase']='agent_reviewed_candidate';s['completion_evidence']=checklist;save(work/'workflow.json',s);return status(work)

def status(work):
 s=verified(work);return {'phase':s['phase'],'source_checked':sum(t['source_status']=='checked' for t in s['tiles']),'tiles':len(s['tiles']),'qa_checked':sum(t['qa_status']=='checked' for t in s['tiles']),'road_count':len(s['roads']),'tool_tasks':len(s['tasks']),'unresolved':s['unresolved'],'candidate_sha256':s['candidate_sha256'],'complete':s['phase']=='agent_reviewed_candidate','note':'Completion is an agent review claim, not independent pixel truth.'}

def reopen(work,reason):
 work=Path(work);s=verified(work)
 if s['phase']!='agent_reviewed_candidate' or not reason:raise ValueError('final candidate and repair reason required')
 s.setdefault('completion_history',[]).append({'candidate_sha256':s['candidate_sha256'],'completion_evidence':s.pop('completion_evidence'),'reopened_because':reason})
 s['phase']='qa';save(work/'workflow.json',s);return status(work)

def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['init','next','record-source','finish-source','propose','object-propose','apply','extend','begin-qa','record-qa','return-tools','resolve','finalize','status']);p.add_argument('--work',required=True);p.add_argument('--source');p.add_argument('--baseline');p.add_argument('--tile',type=int,default=1536);p.add_argument('--overlap',type=int,default=256);p.add_argument('--tile-id');p.add_argument('--json');p.add_argument('--road-id');p.add_argument('--task-id');p.add_argument('--objects');a=p.parse_args()
 if a.command=='init':result=init(a.work,a.source,a.baseline,a.tile,a.overlap);result={'phase':result['phase'],'tiles':len(result['tiles']),'source_sha256':result['source_sha256']}
 elif a.command=='next':result=next_task(a.work)
 elif a.command=='record-source':result=record_source(a.work,a.tile_id,load(a.json))
 elif a.command=='finish-source':result=finish_source(a.work,load(a.json))
 elif a.command=='propose':result=create_proposal(a.work,a.road_id,load(a.json))
 elif a.command=='object-propose':result=propose_object(a.work,a.task_id,load(a.json))
 elif a.command=='apply':result=apply_task(a.work,a.task_id,load(a.json),load(a.objects) if a.objects else [])
 elif a.command=='extend':result=extend_task(a.work,load(a.json))
 elif a.command=='begin-qa':result=begin_qa(a.work)
 elif a.command=='record-qa':result=record_qa(a.work,a.tile_id,load(a.json))
 elif a.command=='return-tools':result=return_to_tools(a.work)
 elif a.command=='resolve':result=resolve_issue(a.work,load(a.json))
 elif a.command=='finalize':result=finalize(a.work,load(a.json))
 else:result=status(a.work)
 print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
