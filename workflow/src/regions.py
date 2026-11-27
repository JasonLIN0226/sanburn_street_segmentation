"""Source-only agent geometry -> image-boundary refinement -> visual QA.

The program cannot discover streets or author review receipts.  The operator
must view the source tiles, supply their observations and the street geometry,
then view the generated QA tiles.  Zero initialization is mandatory here.
"""
from pathlib import Path
import argparse,json,hashlib
import cv2
import numpy as np
from PIL import Image,ImageDraw
import workflow as wf
from corridor import _path
from junction_checks import possible_junction_gaps
Image.MAX_IMAGE_PIXELS=None
ROOT=Path(__file__).resolve().parents[1]

def read(p):return json.loads(Path(p).read_text())
def write(p,v):wf.save(p,v)

def ingest(name):
 work=ROOT/'maps'/name;s=wf.verified(work);d=read(ROOT/'reviews'/f'{name}_source.json')
 if s['baseline'] is not None:raise ValueError('new run must start from zero')
 if set(d['tiles'])!={t['id'] for t in s['tiles']}:raise ValueError('every native tile needs an observation')
 # next-task image generation is unnecessary: source crops already exist.
 wf.next_task=lambda work:{'saved':True}
 for t in s['tiles']:
  if t['source_status']=='checked':continue
  v=d['tiles'][t['id']];a,b,c,e=t['box'];cs=[]
  for rid in v['roads']:
   r=next(r for r in d['roads'] if r['id']==rid);pts=np.array(r['polygon']);lo=pts.min(0);hi=pts.max(0)
   box=[max(a,int(lo[0])),max(b,int(lo[1])),min(c,int(hi[0])+1),min(e,int(hi[1])+1)]
   if box[0]>=box[2] or box[1]>=box[3]:raise ValueError(f'{rid} does not intersect {t["id"]}')
   cs.append({'id':rid,'classification':'road','box':box,'evidence':v['observation'],'continuation':r['continuation'],'obstacles':r.get('obstacles',[])})
  cs.append({'classification':'nonroad','box':t['box'],'evidence':v['nonroad'],'scope':'Evidence concerns named nonroad objects, not the entire bounding box.'})
  wf.record_source(work,t['id'],{'source_sha256':s['source_sha256'],'crop_sha256':wf.sha(work/'source_tiles'/(t['id']+'.png')),'box':t['box'],'observation':v['observation'],'candidates':cs,'edge_evidence':{side:v['edge'] for side in t['edge_sides']}})
 roads=[{**r,'exits':[dict(x,status='pending') for x in r.get('exits',[])]} for r in d['roads']]
 wf.finish_source(work,{'roads':roads,'aliases':{},'unresolved':d.get('unresolved',[])})
 print(name,'source reviewed',len(s['tiles']),'roads',len(roads),flush=True)

def snap_edge(gray,a,b,radius,weight):
 a=np.array(a,float);b=np.array(b,float);delta=b-a;n=max(2,int(np.linalg.norm(delta))+1)
 pts=a+np.linspace(0,1,n)[:,None]*delta
 norm=np.array([-delta[1],delta[0]])/max(np.linalg.norm(delta),1)
 off=np.arange(-radius,radius+1);xy=pts[:,None,:]+off[None,:,None]*norm
 xx=np.clip(np.rint(xy[:,:,0]).astype(int),0,gray.shape[1]-1);yy=np.clip(np.rint(xy[:,:,1]).astype(int),0,gray.shape[0]-1)
 ink=gray[yy,xx].astype(float)/255
 cost=ink+weight*np.abs(off)[None,:]
 ix,score=_path(cost);out=pts+off[ix,None]*norm
 # Corner anchors remain geometrically shared; no seams between edges.
 k=min(16,n//3)
 if k:
  t=np.linspace(0,1,k)[:,None];out[:k]=pts[:k]*(1-t)+out[:k]*t;out[-k:]=out[-k:]*(1-t)+pts[-k:]*t
 return np.rint(out).astype(np.int32),{'mean_cost':score,'unsupported_fraction':float((ink[np.arange(n),ix]>.65).mean())}

def build(name,variant='balanced',revision='r1'):
 wf.safe_id(name);wf.safe_id(revision)
 work=ROOT/'maps'/name;s=wf.verified(work)
 if s['phase'] not in ('tools','qa'):raise ValueError('source inventory required')
 if any(t['source_status']!='checked' for t in s['tiles']):raise ValueError('source review incomplete')
 if (work/'tools'/revision).exists():raise FileExistsError('revision already exists')
 if s['phase']=='qa':
  archive=work/'tools'/('qa_before_'+revision+'.json')
  write(archive,{'candidate_sha256':s['candidate_sha256'],'tiles':s['tiles']})
  wf.next_task=lambda work:{};wf.return_to_tools(work);s=wf.verified(work)
 d=read(ROOT/'reviews'/f'{name}_source.json')
 if {r['id'] for r in d['roads']}!={r['id'] for r in s['roads']}:raise ValueError('tool road IDs must match reviewed source inventory')
 rgb=np.array(Image.open(s['source']).convert('RGB'));gray=cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
 # Blur only across a few source pixels to prefer actual line support over noise.
 gray=cv2.GaussianBlur(gray,(3,3),.6);mask=np.zeros(gray.shape,np.uint8);stats=[]
 radius,weight={'tight':(15,.035),'balanced':(32,.022),'wide':(50,.018)}[variant]
 out=work/'tools'/revision;out.mkdir(exist_ok=False)
 (out/'agent_input.json').write_bytes((ROOT/'reviews'/f'{name}_source.json').read_bytes())
 write(out/'implementation.json',{p.name:wf.sha(p) for p in Path(__file__).parent.glob('*.py')})
 for r in d['roads']:
  pts=r['polygon'];edges=[];es=[]
  fixed=set(r.get('fixed_edges',[]))
  for j,a in enumerate(pts):
   b=pts[(j+1)%len(pts)]
   if j in fixed:edge=np.array([a,b],np.int32);info={'kind':'inferred_or_junction','reason':r.get('fixed_reason','source-reviewed continuation/cross section')}
   else:edge,info=snap_edge(gray,a,b,int(r.get('radius',radius)),weight);info['kind']='source_ink_snap'
   edges.append(edge);es.append(info)
  poly=np.concatenate(edges);cv2.fillPoly(mask,[poly],255)
  stats.append({'road_id':r['id'],'edge_evidence':es,'polygon_after':poly[::max(1,len(poly)//250)].tolist()})
 # Only opt-in enclosed numerical holes in the road union; explicit physical
 # obstacles are subtracted afterwards and therefore cannot be filled back.
 if d.get('fill_enclosed_holes_max_area',0):
  count,labels,components,_=cv2.connectedComponentsWithStats(255-mask,8)
  border=set(np.unique(np.concatenate([labels[0],labels[-1],labels[:,0],labels[:,-1]])))
  for j in range(1,count):
   if j not in border and components[j,cv2.CC_STAT_AREA]<=int(d['fill_enclosed_holes_max_area']):mask[labels==j]=255
 review_locations = possible_junction_gaps(mask, gray, d['roads'], d.get('exclusions', []))
 for x in d.get('exclusions',[]):
  pts=x['polygon'];edges=[]
  for j,a in enumerate(pts):
   e,_=snap_edge(gray,a,pts[(j+1)%len(pts)],x.get('radius',10),.035);edges.append(e)
  poly=np.concatenate(edges)
  if x.get('trim_to_blue'):
   # A reviewer identified water inside this local polygon. Restrict removal
   # to blue source pixels; this is not global water or road detection.
   lo=np.maximum(poly.min(0)-24,0);hi=np.minimum(poly.max(0)+25,[mask.shape[1],mask.shape[0]])
   x0,y0=lo;x1,y1=hi;patch=rgb[y0:y1,x0:x1].astype(np.int16)
   blue=((patch[:,:,2]-patch[:,:,0]>15)&(patch[:,:,1]-patch[:,:,0]>10)&(patch[:,:,2]>100)).astype(np.uint8)*255
   blue=cv2.morphologyEx(blue,cv2.MORPH_CLOSE,np.ones((5,5),np.uint8))
   blue=cv2.dilate(blue,np.ones((3,3),np.uint8))
   gate=np.zeros_like(blue);cv2.fillPoly(gate,[poly-lo],255)
   mask[y0:y1,x0:x1][(gate>0)&(blue>0)]=0
  elif x.get('trim_to_yellow'):
   # This is a local footprint constraint, never a street detector. The agent
   # must explicitly classify this object as a yellow aboveground projection.
   lo=np.maximum(poly.min(0)-24,0);hi=np.minimum(poly.max(0)+25,[mask.shape[1],mask.shape[0]])
   x0,y0=lo;x1,y1=hi;patch=rgb[y0:y1,x0:x1].astype(np.int16)
   yellow=((patch[:,:,0]-patch[:,:,2]>40)&(patch[:,:,1]-patch[:,:,2]>23)&(patch[:,:,2]<232)).astype(np.uint8)*255
   yellow=cv2.morphologyEx(yellow,cv2.MORPH_CLOSE,np.ones((13,13),np.uint8))
   # Closing/contour fill can bridge an outlined building and mistakenly
   # classify an entire courtyard or neighbouring road as yellow.  Preserve
   # the pixel support unless the reviewer explicitly opts into contour fill.
   if x.get('fill_yellow_contours',False):
    contours,_=cv2.findContours(yellow,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
    support=np.zeros(yellow.shape,np.uint8)
    cv2.drawContours(support,[c for c in contours if cv2.contourArea(c)>30],-1,255,-1)
   else:
    support=yellow
   support=cv2.dilate(support,np.ones((5,5),np.uint8))
   gate=np.zeros_like(support);cv2.fillPoly(gate,[poly-lo],255)
   mask[y0:y1,x0:x1][(gate>0)&(support>0)]=0
  else:cv2.fillPoly(mask,[poly],0)
 # Explicit agent paper polygon clips only photographed background.
 if d.get('paper_polygon'):
  pp=d['paper_polygon'];pedges=[]
  paper_cost=gray
  if d.get('paper_edge_gradient'):
   # Paper/background is a brightness transition, not a printed black line.
   gx=cv2.convertScaleAbs(cv2.Sobel(gray,cv2.CV_16S,1,0,ksize=3))
   gy=cv2.convertScaleAbs(cv2.Sobel(gray,cv2.CV_16S,0,1,ksize=3))
   paper_cost=255-cv2.max(gx,gy);del gx,gy
  for j,a in enumerate(pp):
   edge,_=snap_edge(paper_cost,a,pp[(j+1)%len(pp)],d.get('paper_snap_radius',30),.025);pedges.append(edge)
  paper=np.zeros_like(mask);cv2.fillPoly(paper,[np.concatenate(pedges)],255);mask=cv2.bitwise_and(mask,paper)
  # A photographed sheet can have a dark binding below its physical edge.
  # Allow the reviewer to conservatively inset only the detected bottom edge;
  # this deliberately leaves other sheet exits untouched.
  profile=d.get('paper_bottom_inset_profile')
  bottom_inset=int(d.get('paper_bottom_inset',0))
  if bottom_inset or profile:
   xs=np.arange(mask.shape[1]);bottom=paper.shape[0]-1-np.argmax(paper[::-1,:]>0,axis=0)
   insets=(np.rint(np.interp(xs,[p[0] for p in profile],[p[1] for p in profile])).astype(int)
           if profile else np.full(mask.shape[1],bottom_inset,dtype=int))
   for offset in range(int(insets.max())):
    cols=xs[insets>offset];ys=np.maximum(bottom[cols]-offset,0)
    mask[ys,cols]=0
  Image.fromarray(paper).save(out/'paper.png')
 Image.fromarray(mask).save(out/'mask.png');write(out/'proposal.json',{'source_sha256':s['source_sha256'],'candidate_before_sha256':s['candidate_sha256'],'agent_input_sha256':wf.sha(ROOT/'reviews'/f'{name}_source.json'),'variant':variant,'roads':stats,'review_locations':review_locations})
 a=rgb.copy();ix=mask>0;a[ix]=(a[ix]*.48+np.array([20,140,240])*.52).astype(np.uint8)
 Image.fromarray(a).save(out/'overlay.jpg',quality=92)
 im=Image.fromarray(a);im.thumbnail((1600,1800));im.save(out/'overview.jpg',quality=93)
 for t in s['tiles']:Image.fromarray(a[t['box'][1]:t['box'][3],t['box'][0]:t['box'][2]]).save(out/(t['id']+'.jpg'),quality=94)
 print(name,revision,variant,'proposal ready; requires visual selection',flush=True)

def select(name,revision,evidence):
 work=ROOT/'maps'/name;s=wf.verified(work);out=work/'tools'/revision;p=read(out/'proposal.json')
 if s['phase']!='tools' or p['source_sha256']!=s['source_sha256'] or p['candidate_before_sha256']!=s['candidate_sha256']:raise ValueError('stale proposal')
 if p['agent_input_sha256']!=wf.sha(ROOT/'reviews'/f'{name}_source.json'):raise ValueError('geometry changed since build')
 if not evidence:raise ValueError('visual selection evidence required')
 (work/'candidate.png').write_bytes((out/'mask.png').read_bytes());s['candidate_sha256']=wf.sha(work/'candidate.png')
 s['tasks'].append({'task_id':revision,'kind':'agent_regions_source_ink_refinement','road_ids':[r['road_id'] for r in p['roads']],'proposal_sha256':wf.sha(out/'proposal.json'),'selection_evidence':evidence,'candidate_sha256':s['candidate_sha256']})
 # Source inventory and QA manifests are versioned. No prior QA survives edits.
 for t in s['tiles']:t['qa_status']='pending';t.pop('qa_receipt',None)
 s['phase']='qa';s['qa_candidate_sha256']=s['candidate_sha256'];write(work/'workflow.json',s)
 rgb=np.array(Image.open(s['source']).convert('RGB'));m=np.array(Image.open(work/'candidate.png'))>0;rgb[m]=(rgb[m]*.48+np.array([20,140,240])*.52).astype(np.uint8)
 for t in s['tiles']:Image.fromarray(rgb[t['box'][1]:t['box'][3],t['box'][0]:t['box'][2]]).save(work/'qa_tiles'/(t['id']+'.png'))
 print(name,'selected',revision,'QA pending',len(s['tiles']),flush=True)

def qa(name):
 work=ROOT/'maps'/name;s=wf.verified(work);d=read(ROOT/'reviews'/f'{name}_qa.json')
 if d['candidate_sha256']!=s['candidate_sha256']:raise ValueError('QA receipt is stale')
 if set(d['tiles'])!={t['id'] for t in s['tiles']}:raise ValueError('all tiles required')
 wf.next_task=lambda work:{}
 for t in s['tiles']:
  if t['qa_status']=='checked':continue
  v=d['tiles'][t['id']]
  provenance=v.get('unchanged_from')
  if provenance:
   old=read(work/'tools'/provenance);ot=next(x for x in old['tiles'] if x['id']==t['id'])
   if ot.get('qa_status')!='checked' or ot['qa_receipt']['crop_sha256']!=wf.sha(work/'qa_tiles'/(t['id']+'.png')):raise ValueError('cannot reuse QA: pixels changed or prior review missing')
  wf.record_qa(work,t['id'],{'candidate_sha256':s['candidate_sha256'],'crop_sha256':wf.sha(work/'qa_tiles'/(t['id']+'.png')),'observation':v['observation'],'issues':v.get('issues',[]),'no_issue_evidence':v.get('evidence',v['observation']),'edge_evidence':{side:v['edge'] for side in t['edge_sides']},'unchanged_from':provenance})
 # Exit resolution must be explicitly supplied per road/exit, after QA.
 for key,evidence in d.get('exit_resolutions',{}).items():
  rid,idx=key.rsplit(':',1);s=wf.verified(work);i=next((i for i,u in enumerate(s['unresolved']) if u.get('type')=='inventory_exit' and u['road_id']==rid and u['exit_index']==int(idx)),None)
  if i is not None:wf.resolve_issue(work,{'index':i,'candidate_sha256':s['candidate_sha256'],'exit_status':'labelled','evidence':evidence})
 if d.get('finalize'):
  wf.finalize(work,{'roads':d['roads'],'edge_evidence':d['edge_evidence'],'limitations':d['limitations']})
 print(wf.status(work),flush=True)

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('action',choices=['ingest','build','select','qa']);p.add_argument('name');p.add_argument('--variant',default='balanced');p.add_argument('--revision',default='r1');p.add_argument('--evidence');a=p.parse_args()
 if a.action=='ingest':ingest(a.name)
 elif a.action=='build':build(a.name,a.variant,a.revision)
 elif a.action=='select':select(a.name,a.revision,a.evidence)
 else:qa(a.name)
