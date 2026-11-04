"""Agent supplied boundary pairs and explicit evidence; no road detection model.

The two polylines are ordered in the same longitudinal direction. Coordinates
are in the declared visual reference frame and are mapped to source pixels.
Junctions, source-edge continuations and obstacle decisions remain the agent's
responsibility; candidate diagnostics are review prompts, not ground truth.
"""
import json
import math
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

Image.MAX_IMAGE_PIXELS = None


def _cross(a, b, c):
    return float((b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]))


def _intersects(a, b, c, d):
    x1, x2, x3, x4 = _cross(a,b,c), _cross(a,b,d), _cross(c,d,a), _cross(c,d,b)
    return x1*x2 < 0 and x3*x4 < 0


def apply_pairs(inventory, spec, size):
    if spec.get('coordinate_system') != 'reference_xy':
        raise ValueError('explicit visual reference coordinate system required')
    rw, rh = spec['reference_wh']
    if rw <= 0 or rh <= 0:
        raise ValueError('invalid reference size')
    width, height = size
    by_id = {r['id']: r for r in inventory['roads']}
    seen = set()
    for road in spec['roads']:
        if road['road_id'] not in by_id:
            raise ValueError('unknown road id')
        if road['road_id'] in seen:
            raise ValueError('duplicate boundary pair for road')
        seen.add(road['road_id'])
        if not road.get('evidence') or not road.get('endpoints_evidence'):
            raise ValueError('boundary and endpoint evidence required')
        a, b = [np.array(road[k], dtype=float) for k in ('side_a', 'side_b')]
        if any(x.ndim != 2 or x.shape[1] != 2 or len(x) < 2 for x in (a, b)):
            raise ValueError('two polylines with at least two points required')
        if any(np.any(x[:, 0] < 0) or np.any(x[:, 0] >= rw) or np.any(x[:, 1] < 0) or np.any(x[:, 1] >= rh) for x in (a, b)):
            raise ValueError('boundary coordinate outside reference frame')
        if np.dot(a[-1]-a[0], b[-1]-b[0]) <= 0:
            raise ValueError('both sides must run in the same direction')
        pts = np.concatenate((a, b[::-1]))
        pts = np.rint(pts * np.array([width/rw, height/rh])).astype(np.int32)
        if cv2.contourArea(pts) < 100:
            raise ValueError('corridor too small or invalid')
        for i in range(len(pts)):
            for j in range(i+1, len(pts)):
                if j in (i, i+1) or (i == 0 and j == len(pts)-1):
                    continue
                if _intersects(pts[i], pts[(i+1)%len(pts)], pts[j], pts[(j+1)%len(pts)]):
                    raise ValueError('self-intersecting road boundary')
        r = by_id[road['road_id']]
        r['polygon'] = pts.tolist()
        # Caps define ends/junction cross-sections, not printed curb lines.
        r['fixed_edges'] = [len(a)-1, len(pts)-1]
        r['evidence'] += ' Boundary-pair review: ' + road['evidence']
        r['continuation'] = road['endpoints_evidence']
        r['boundary_pair_id'] = road['road_id']
    return inventory


def boundary_evidence(source, spec):
    """Approximate line support by segment; weak ink means review, not rejection."""
    image = np.asarray(Image.open(source).convert('L'))
    height, width = image.shape
    rw, rh = spec['reference_wh']
    reports = []
    for road in spec['roads']:
        sides = {}
        for key in ('side_a', 'side_b'):
            points = np.asarray(road[key], float) * [width/rw, height/rh]
            segments = []
            for p, q in zip(points[:-1], points[1:]):
                n = max(2, math.ceil(float(np.linalg.norm(q-p))/8))
                xy = p + np.linspace(0, 1, n)[:, None]*(q-p)
                # Agent guides can be slightly off; sample a small neighborhood.
                ix = np.clip(np.rint(xy[:, 0]).astype(int), 3, width-4)
                iy = np.clip(np.rint(xy[:, 1]).astype(int), 3, height-4)
                darkness = np.stack([image[iy+dy, ix+dx] for dy in range(-3,4,3) for dx in range(-3,4,3)]).min(axis=0)
                supported = darkness < 145
                longest = run = 0
                for v in supported:
                    run = 0 if v else run+1
                    longest = max(longest, run)
                segments.append({'from': (p/[width/rw,height/rh]).round(1).tolist(),
                                 'to': (q/[width/rw,height/rh]).round(1).tolist(),
                                 'dark_support_fraction': round(float(supported.mean()), 3),
                                 'longest_unsupported_approx_pixels': longest*8,
                                 'review_needed': bool(supported.mean() < .55 or longest*8 > 320)})
            sides[key] = segments
        reports.append({'road_id': road['road_id'], 'sides': sides,
                        'caution': 'Dark lines can be text or parcel boundaries; weak support can also occur at junctions.'})
    return reports


def review_gate(review):
    """Never label a map passed if high-severity observations remain unresolved."""
    sides = ('north', 'east', 'south', 'west')
    edges = review.get('edge_sweep', {})
    missing = []
    uncertain = []
    for side in sides:
        entry = edges.get(side, {})
        segments = entry.get('segments', [])
        if not entry.get('evidence') or not segments:
            missing.append(side)
            continue
        cursor = 0.0
        for seg in sorted(segments, key=lambda x: x.get('start', -1)):
            a, b = seg.get('start', -1), seg.get('end', -1)
            if (not 0 <= a < b <= 1 or a > cursor + 1e-6 or
                seg.get('classification') not in ('road', 'nonroad', 'uncertain') or
                not seg.get('evidence')):
                missing.append(side)
                break
            if seg['classification'] == 'road' and (not seg.get('road_id') or seg.get('mask_status') != 'connected_to_edge'):
                uncertain.append({'side': side, 'segment': seg, 'reason': 'road exit not confirmed connected'})
            if seg['classification'] == 'uncertain':
                uncertain.append({'side': side, 'segment': seg, 'reason': 'unresolved interpretation'})
            cursor = max(cursor, b)
        if cursor < 1-1e-6:
            missing.append(side)
    issues = review.get('issues', [])
    pending = [i for i in issues if i.get('severity') == 'major' and i.get('status') not in ('repaired_verified', 'not_issue_verified')]
    invalid = [i for i in issues if not i.get('location') or not i.get('evidence') or not i.get('status') or i.get('severity') not in ('major','minor') or (i.get('severity') == 'major' and i.get('status') in ('repaired_verified','not_issue_verified') and not i.get('resolution_evidence'))]
    return {'status': 'needs_review' if missing or invalid or pending or uncertain else 'reviewed',
            'unreviewed_edges': sorted(set(missing)), 'invalid_issues': len(invalid),
            'unresolved_edge_segments': uncertain, 'unresolved_major': pending,
            'note': 'The gate enforces review bookkeeping; it cannot detect errors or prove accuracy.'}


def possible_yellow_overlap(source, mask_path, tile=1024, min_area=100):
    """Report candidate overlaps to inspect, without changing the output mask.

    Yellow can be building paint, road paint, discoloration, or text. A color
    match is not a building detector and is never a semantic decision.
    """
    image = Image.open(source).convert('RGB')
    mask = Image.open(mask_path).convert('L')
    if image.size != mask.size:
        raise ValueError('source and mask differ in size')
    boxes = []
    for y in range(0, image.height, tile):
        for x in range(0, image.width, tile):
            box = (x, y, min(x+tile, image.width), min(y+tile, image.height))
            rgb = np.asarray(image.crop(box)).astype(np.int16)
            m = np.asarray(mask.crop(box)) > 0
            yellow = ((rgb[:,:,0]-rgb[:,:,2] > 40) & (rgb[:,:,1]-rgb[:,:,2] > 23) & (rgb[:,:,2] < 232) & m).astype(np.uint8)
            n, _, stats, _ = cv2.connectedComponentsWithStats(yellow, 8)
            for left, top, w, h, area in stats[1:n]:
                if area >= min_area:
                    boxes.append({'box':[int(x+left), int(y+top), int(x+left+w), int(y+top+h)], 'support_pixels':int(area)})
    return {'candidates': sorted(boxes, key=lambda item: item['support_pixels'], reverse=True),
            'warning':'Color-based hints have false positives and false negatives; review against the original image.'}
