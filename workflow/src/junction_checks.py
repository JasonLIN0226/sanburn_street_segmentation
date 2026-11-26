"""Locate possible omissions between converging streets for local inspection."""
from itertools import combinations

import cv2
import numpy as np


def cross(a, b):
    return a[0] * b[1] - a[1] * b[0]


def junction_triangle(first, second):
    a, b = first
    c, d = second
    ab, cd = b - a, d - c
    denominator = cross(ab, cd)
    if not denominator:
        return None
    u = cross(c - a, cd) / denominator
    v = cross(c - a, ab) / denominator
    # At a merge the facing segments meet near their tips, not in mid-crossing.
    if not all(0 <= t <= 1 and min(t, 1-t) <= .25 for t in (u, v)):
        return None
    left, right = (a if u > .5 else b), (c if v > .5 else d)
    tip = a + u * ab
    base = right - left
    if abs(cross(base, tip-left)) <= np.dot(base, base):
        return None
    return np.float32([left, right, tip])


def ink_support(gray, a, b, radius, dark):
    tangent = b-a
    normal = np.array([-tangent[1], tangent[0]]) / np.linalg.norm(tangent)
    points = a + np.linspace(.1, .9, 48)[:, None] * tangent
    samples = points[:, None] + np.arange(-radius, radius+1)[None, :, None] * normal
    x, y = np.rint(samples).astype(int).T
    x = np.clip(x, 0, gray.shape[1]-1)
    y = np.clip(y, 0, gray.shape[0]-1)
    return np.mean(gray[y, x].min(axis=0) < dark)


def possible_junction_gaps(mask, gray, roads, exclusions):
    """Read the road union before exclusions. Never write to the union."""
    footprints = [np.array(item['polygon'], np.int32) for item in exclusions]
    if not footprints:
        return []
    segments = []
    for road in roads:
        points = np.asarray(road['polygon'], dtype=float)
        fixed = set(road.get('fixed_edges', []))
        for j in range(len(points)):
            if j in fixed:
                segments.append((road, points[[j, (j+1) % len(points)]]))

    findings = []
    for (road, first), (other, second) in combinations(segments, 2):
        if road is other or np.any(np.maximum(first.min(0), second.min(0)) >
                                   np.minimum(first.max(0), second.max(0))):
            continue
        tri = junction_triangle(first, second)
        if tri is None:
            continue
        enclosed = [i for i, ring in enumerate(footprints) if all(
            cv2.pointPolygonTest(tri, (float(x), float(y)), False) > 0 for x, y in ring)]
        if not enclosed:
            continue
        x, y, w, h = cv2.boundingRect(tri)
        roi = mask[y:y+h, x:x+w]
        stencil = np.zeros(roi.shape, np.uint8)
        cv2.fillConvexPoly(stencil, np.rint(tri - [x, y]).astype(np.int32), 1)
        # The rasterized street outlines share the triangle's perimeter.
        # Count its interior independently of how those edges were rounded.
        inside = cv2.erode(stencil, np.ones((3, 3), np.uint8),
                           borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
        if not inside.any() or np.mean(roi[inside] > 0) > .05:
            continue
        a, b, tip = tri
        radius = max(1, min(road.get('radius', 32), other.get('radius', 32),
                            int(np.linalg.norm(a-b) / 12)))
        dark = float(np.median(gray[y:y+h, x:x+w][inside])) - 40
        if (ink_support(gray,a,b,radius,dark) >= .85 and
                max(ink_support(gray,a,tip,radius,dark), ink_support(gray,b,tip,radius,dark)) <= .25):
            findings.append({'kind': 'possible_open_junction', 'roads': [road['id'], other['id']],
                             'exclusion_indices': enclosed, 'native_triangle': np.rint(tri).astype(int).tolist()})
    return findings
