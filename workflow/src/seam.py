"""Seam-aware merging; based on prior and candidate, uncertainty preserved."""
import numpy as np

def _run(row,center):
 if not row[center]:raise ValueError('no continuous old and new road at center; defer')
 left=right=center
 while left>0 and row[left-1]:left-=1
 while right+1<len(row) and row[right+1]:right+=1
 return left,right

def transition(before,after,spec,paths,selection,width=32,max_width_delta=40):
 if before.shape!=after.shape or width<2:raise ValueError('invalid transition')
 start,end=spec['span'];axis=spec['axis'];out=after.copy();uncertain=np.zeros_like(out,np.uint8);n=end-start+1
 if width*2>n:raise ValueError('transition exceeds segment')
 lo=paths[selection['boundary_ids']['low']];hi=paths[selection['boundary_ids']['high']]
 bands=[]
 for at,order in [(start,range(width)),(end,range(width))]:
  valid=at>0 if at==start else at+1<(before.shape[0] if axis=='y' else before.shape[1])
  if not valid:continue
  for j in order:
   t=start+j if at==start else end-j;i=t-start;center=int((lo[i]+hi[i])//2)
   old=before[t,:] if axis=='y' else before[:,t];new=after[t,:] if axis=='y' else after[:,t];row=out[t,:] if axis=='y' else out[:,t];u=uncertain[t,:] if axis=='y' else uncertain[:,t]
   a,b=_run(old,center);c,d=_run(new,center)
   if max(abs(a-c),abs(b-d))>max_width_delta:raise ValueError('old/new boundary disagreement too large; defer')
   alpha=j/(width-1);left=round((1-alpha)*a+alpha*c);right=round((1-alpha)*b+alpha*d)
   lower=min(a,c);upper=max(b,d)+1;row[lower:upper]=0;row[left:right+1]=1
   if j==0:row[:]=old
   u[lower:upper]=2
  bands.append([start,start+width-1] if at==start else [end-width+1,end])
 return out,uncertain,{'transition_ranges':bands,'status':'inferred_requires_review'}
