"""Agent-proposed nonroad object candidates within agent-reviewed boxes."""
import cv2
import numpy as np

def propose_exclusion(rgb, box, seed):
 x0,y0,x1,y1=box;x,y=seed
 if not(0<=x0<=x<x1<=rgb.shape[1] and 0<=y0<=y<y1<=rgb.shape[0]):raise ValueError('object seed outside box')
 a=rgb[y0:y1,x0:x1];hsv=cv2.cvtColor(a,cv2.COLOR_RGB2HSV);h,s,v=map(int,hsv[y-y0,x-x0])
 if s<55 or v<80:raise ValueError('seed has weak color evidence')
 hd=np.abs(hsv[:,:,0].astype(int)-h);hd=np.minimum(hd,180-hd)
 pick=((hd<=10)&(hsv[:,:,1]>=max(45,int(s*.50)))&(hsv[:,:,2]>=80)).astype('uint8')
 pick=cv2.morphologyEx(pick,cv2.MORPH_CLOSE,np.ones((5,5),np.uint8))
 _,components=cv2.connectedComponents(pick,connectivity=8);label=components[y-y0,x-x0]
 if not label:raise ValueError('seed not in component')
 selected=(components==label).astype('uint8');contours,_=cv2.findContours(selected,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE);filled=np.zeros_like(selected);cv2.drawContours(filled,contours,-1,1,cv2.FILLED)
 # Approximation: never extend beyond the agent-provided task box.
 result=np.zeros(rgb.shape[:2],np.uint8);result[y0:y1,x0:x1]=filled
 return result.astype(bool),{'seed_hsv':[h,s,v],'pixels':int(filled.sum()),'box':box,'status':'needs_agent_visual_confirmation'}

def apply_exclusion(mask,candidate,decision):
 if not decision.get('visual_confirmed') or not decision.get('rationale'):raise ValueError('visual confirmation required')
 if mask.shape!=candidate.shape:raise ValueError('shape mismatch')
 result=mask.copy();result[candidate]=0
 return result,{'removed':int(np.count_nonzero((mask>0)&candidate)),'status':'approximate_needs_QA'}
