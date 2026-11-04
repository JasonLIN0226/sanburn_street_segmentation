"""Generic agent-guided road corridor proposals and bounded reconstruction.

No semantic recognition: the agent provides a source-only road corridor, two
coarse boundary guides, seed points and an explicit candidate selection.
"""
import hashlib, json
import numpy as np


def spec_hash(spec):
 return hashlib.sha256(json.dumps(spec,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def path_hash(paths):
 h=hashlib.sha256()
 for k in sorted(paths):
  h.update(k.encode());h.update(np.asarray(paths[k],dtype='<i4').tobytes())
 return h.hexdigest()


def _runs(flags,start):
 runs=[];begin=None
 for i,value in enumerate(flags):
  if value and begin is None:begin=start+i
  if not value and begin is not None:runs.append([begin,start+i-1]);begin=None
 if begin is not None:runs.append([begin,start+len(flags)-1])
 return runs


def _path(cost):
 n,k=cost.shape;back=np.zeros((n,k),np.int8);prev=cost[0].copy()
 for t in range(1,n):
  shifts=np.stack([np.r_[np.inf,prev[:-1]]+.18,prev,np.r_[prev[1:],np.inf]+.18],axis=0)
  parent=np.argmin(shifts,axis=0);back[t]=parent.astype(np.int8)-1
  prev=shifts[parent,np.arange(k)]+cost[t]
 p=np.empty(n,np.int32);p[-1]=np.argmin(prev)
 for t in range(n-1,0,-1):p[t-1]=p[t]+int(back[t,p[t]])
 return p,float(prev[p[-1]]/n)


def propose(gray,spec):
 axis=spec['axis'];start,end=spec['span'];radius=int(spec['radius']);guide=spec['guides']
 if axis not in ('x','y') or start<0 or end<start or radius<4:raise ValueError('invalid task')
 along_max=gray.shape[0] if axis=='y' else gray.shape[1];across_max=gray.shape[1] if axis=='y' else gray.shape[0]
 if end>=along_max:raise ValueError('outside image')
 ts=np.arange(start,end+1);offs=np.arange(-radius,radius+1);result={};paths={}
 for side in ('low','high'):
  anchors=np.asarray(guide[side],float)
  if anchors.ndim!=2 or anchors.shape[1]!=2 or np.any(np.diff(anchors[:,0])<=0) or anchors[0,0]>start or anchors[-1,0]<end:raise ValueError('guide must span task')
  center=np.rint(np.interp(ts,anchors[:,0],anchors[:,1])).astype(int);loc=center[:,None]+offs
  if loc.min()<0 or loc.max()>=across_max:raise ValueError('search exceeds image')
  ink=(gray[ts[:,None],loc] if axis=='y' else gray[loc,ts[:,None]]).astype(float)
  base=ink/255.0 + .012*np.abs(offs)[None,:];cost=base.copy();items=[]
  for i in range(3):
   ix,score=_path(cost);xy=loc[np.arange(len(ts)),ix];val=ink[np.arange(len(ts)),ix];bad=val>=150
   key=f'{side}_{i}';paths[key]=xy
   items.append({'id':key,'score':round(score,4),'dark_fraction':round(float((~bad).mean()),4),'unsupported':_runs(bad,start),'median_coordinate':float(np.median(xy))})
   cost[np.abs(loc-xy[:,None])<=3]+=4.0
  result[side]=items
 return {'spec_sha256':spec_hash(spec),'gray_sha256':hashlib.sha256(gray.tobytes()).hexdigest(),'paths_sha256':path_hash(paths),'candidates':result},paths


def reconstruct(mask,spec,proposal,paths,selection):
 """Reconstruct only reviewed section; requires explicit choice and safety seeds."""
 if proposal['spec_sha256']!=spec_hash(spec) or selection.get('spec_sha256')!=proposal['spec_sha256']:raise ValueError('spec mismatch')
 if selection.get('proposal_sha256')!=spec_hash(proposal) or proposal.get('paths_sha256')!=path_hash(paths):raise ValueError('proposal or paths mismatch')
 if not selection.get('rationale'):raise ValueError('selection rationale required')
 axis=spec['axis'];start,end=spec['span'];pad=int(spec['guard']);ids=selection['boundary_ids']
 for side in ('low','high'):
  if ids[side] not in [c['id'] for c in proposal['candidates'][side]]:raise ValueError('unknown choice')
  candidate=next(c for c in proposal['candidates'][side] if c['id']==ids[side]);gaps=candidate['unsupported']
  largest=max((b-a+1 for a,b in gaps),default=0)
  if largest>selection['max_unsupported_run']:raise ValueError('boundary has unreviewed missing evidence')
 lo=np.asarray(paths[ids['low']],int).copy();hi=np.asarray(paths[ids['high']],int).copy()
 if len(lo)!=end-start+1 or len(hi)!=len(lo) or np.any(hi-lo<3):raise ValueError('crossing boundaries')
 blend=int(selection.get('blend_width',0));blend_ranges=[]
 if blend:
  if not selection.get('blend_rationale'):raise ValueError('blend requires explicit agent rationale')
  if blend<2 or blend*2>len(lo):raise ValueError('invalid blend width')
  along_limit=mask.shape[0] if axis=='y' else mask.shape[1]
  for at,adj,indices in [(start,start-1,np.arange(blend)),(end,end+1,np.arange(len(lo)-1,len(lo)-blend-1,-1))]:
   if adj<0 or adj>=along_limit:continue
   row=mask[adj,:] if axis=='y' else mask[:,adj];idx=0 if at==start else len(lo)-1
   center=int((lo[idx]+hi[idx])//2)
   if not row[center]:raise ValueError('cannot blend: outside section has no road at center')
   a=center;b=center
   while a>0 and row[a-1]:a-=1
   while b+1<len(row) and row[b+1]:b+=1
   a-=1;b+=1
   if max(abs(a-lo[idx]),abs(b-hi[idx]))>int(selection.get('blend_max_jump',30)):raise ValueError('cannot blend: outside section too different')
   for j,i in enumerate(indices):
    alpha=j/(blend-1);lo[i]=round((1-alpha)*a+alpha*lo[i]);hi[i]=round((1-alpha)*b+alpha*hi[i])
   blend_ranges.append([min(start+int(indices[0]),start+int(indices[-1])),max(start+int(indices[0]),start+int(indices[-1]))])
 if np.any(hi-lo<3):raise ValueError('crossing blended boundaries')
 limits=mask.shape[1] if axis=='y' else mask.shape[0]
 if np.min(lo)-pad<0 or np.max(hi)+pad>=limits:raise ValueError('guard outside image')
 if selection['mode'] not in ('replace','add_only'):raise ValueError('mode')
 if not selection.get('road_seeds') or (selection['mode']=='replace' and not selection.get('background_seeds')):raise ValueError('seeds required')
 out=mask.copy();alerts=[]
 for i,t in enumerate(range(start,end+1)):
  a,b=int(lo[i]),int(hi[i]);row=out[t,:] if axis=='y' else out[:,t];old=row.copy()
  missing=int(np.count_nonzero(old[a+1:b]==0));excess=int(np.count_nonzero(old[a-pad:a]))+int(np.count_nonzero(old[b+1:b+pad+1]))
  if missing or excess:alerts.append({'at':t,'missing':missing,'excess_in_guard':excess})
  row[a+1:b]=1
  if selection['mode']=='replace':row[a-pad:a+1]=0;row[b:b+pad+1]=0
 for field,value in [('road_seeds',1),('background_seeds',0)]:
  for x,y in selection.get(field,[]):
   if not(0<=y<out.shape[0] and 0<=x<out.shape[1]) or out[y,x]!=value:raise ValueError(field+' conflict')
 added=int(np.count_nonzero((out>0)&(mask==0)));removed=int(np.count_nonzero((out==0)&(mask>0)))
 return out,{'added':added,'removed':removed,'alert_rows':len(alerts),'alerts':alerts,'mode':selection['mode'],'uncertain_blend_ranges':blend_ranges}


def extend_exit(mask,paper,axis,anchor,stop,low_at_anchor,high_at_anchor,decision):
 """Extrapolate a reviewed corridor towards a reviewed paper edge.

 This only adds road pixels inside the supplied paper mask; it cannot infer that
 the extrapolation is a road. All affected sections must remain uncertain.
 """
 if axis not in ('x','y') or mask.shape!=paper.shape:raise ValueError('shape or axis')
 if not decision.get('rationale') or not decision.get('exit_confirmed'):raise ValueError('agent confirmation required')
 if stop==anchor or low_at_anchor>=high_at_anchor:raise ValueError('invalid exit')
 step=1 if stop>anchor else -1;along_limit=mask.shape[0] if axis=='y' else mask.shape[1];cross_limit=mask.shape[1] if axis=='y' else mask.shape[0]
 if not(0<=anchor<along_limit and 0<=stop<along_limit and 0<=low_at_anchor<high_at_anchor<cross_limit):raise ValueError('exit outside image')
 anchor_row=mask[anchor,:] if axis=='y' else mask[:,anchor]
 if not anchor_row[(low_at_anchor+high_at_anchor)//2]:raise ValueError('exit anchor does not connect to an existing road')
 out=mask.copy();state=np.zeros_like(mask,np.uint8);added=0;clipped=0
 for t in range(anchor+step,stop+step,step):
  delta=abs(t-anchor);lo=round(low_at_anchor+float(decision.get('low_slope',0))*delta);hi=round(high_at_anchor+float(decision.get('high_slope',0))*delta)
  if not(0<=lo<hi<cross_limit):raise ValueError('extrapolation outside image')
  row=out[t,:] if axis=='y' else out[:,t];pg=paper[t,:] if axis=='y' else paper[:,t];st=state[t,:] if axis=='y' else state[:,t]
  region=np.arange(lo+1,hi);valid=pg[region]>0;clipped+=int((~valid).sum());inside=region[valid]
  added+=int(np.count_nonzero(row[inside]==0));row[inside]=1;st[inside]=2
 return out,state,{'added':added,'paper_clipped':clipped,'status':'inferred'}


def seam_alerts(before,after,spec):
 """Flag abrupt shape changes at the ends of the edited span."""
 start,end=spec['span'];axis=spec['axis'];length=before.shape[0] if axis=='y' else before.shape[1];alerts=[]
 for inside,outside in [(start,start-1),(end,end+1)]:
  if outside<0 or outside>=length:continue
  b_in=before[inside,:] if axis=='y' else before[:,inside];b_out=before[outside,:] if axis=='y' else before[:,outside]
  a_in=after[inside,:] if axis=='y' else after[:,inside];a_out=after[outside,:] if axis=='y' else after[:,outside]
  old=int(np.count_nonzero(b_in!=b_out));new=int(np.count_nonzero(a_in!=a_out))
  if new>old+3:alerts.append({'at':inside,'before_discontinuity':old,'after_discontinuity':new})
 return alerts
