# SPDX-License-Identifier: GPL-3.0-or-later
"""Draw a synthetic "photo" (floor grid + box) from a known camera, for checking
the add-on by eye and in tests. Needs Pillow (runs in Claude's cloud workspace).

Writes tests/data/synthetic_box.png and synthetic_box.json (ground truth).
"""
import json
import math
import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'perspective_match'))
import solver as S  # noqa: E402

W, H = 1800, 1200
ASPECT = W / H
LOCATION = (7.5, -6.0, 1.6)
TARGET = (0.0, 0.5, 0.9)
F = 1.6                    # relative focal length (lens = F * 36 / 2 = 28.8 mm, sensor 36)


def sub(a, b): return tuple(x - y for x, y in zip(a, b))
def cross(a, b): return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
def norm(v):
    n = math.sqrt(sum(c*c for c in v)); return tuple(c/n for c in v)
def dot(a, b): return sum(x*y for x, y in zip(a, b))


fwd = norm(sub(TARGET, LOCATION))
right = norm(cross(fwd, (0, 0, 1)))
up = cross(right, fwd)
VIEW = (right, up, tuple(-c for c in fwd))   # rows: camera axes in world space


def to_cam(p):
    d = sub(p, LOCATION)
    return tuple(dot(r, d) for r in VIEW)


def to_px(p):
    c = to_cam(p)
    if c[2] > -0.05:
        return None
    x, y = F * c[0] / -c[2], F * c[1] / -c[2]
    rx, ry = S.plane_to_rel((x, y), ASPECT)
    return (rx * W, ry * H)


def clip_line(a, b):
    """Clip a 3D segment to the part in front of the camera."""
    ca, cb = to_cam(a), to_cam(b)
    near = -0.1
    if ca[2] > near and cb[2] > near:
        return None
    if ca[2] > near or cb[2] > near:
        t = (near - ca[2]) / (cb[2] - ca[2])
        m = tuple(a[i] + t * (b[i] - a[i]) for i in range(3))
        a, b = (m, b) if ca[2] > near else (a, m)
    return to_px(a), to_px(b)


img = Image.new('RGB', (W, H), (178, 196, 214))
d = ImageDraw.Draw(img)
# ground below the true horizon
far = to_px(tuple(LOCATION[i] + 1e5 * (fwd[0], fwd[1], 0.0)[i] for i in range(3)))
d.rectangle([0, far[1] - LOCATION[2] * 0, W, H], fill=(150, 140, 125))
for i in range(-12, 13):
    for a, b in (((i, -12, 0), (i, 12, 0)), ((-12, i, 0), (12, i, 0))):
        seg = clip_line(a, b)
        if seg and seg[0] and seg[1]:
            d.line([seg[0], seg[1]], fill=(110, 100, 90), width=2)

# box: x 0..2, y 0..1.5, z 0..1.2 (corner at the world origin)
BX, BY, BZ = 2.0, 1.5, 1.2
V = {(i, j, k): (i * BX, j * BY, k * BZ) for i in (0, 1) for j in (0, 1) for k in (0, 1)}
faces = [
    ([(0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)], (0, -1, 0), (205, 120, 80)),
    ([(1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)], (1, 0, 0), (180, 100, 65)),
    ([(0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)], (0, 0, 1), (230, 160, 120)),
    ([(0, 1, 0), (1, 1, 0), (1, 1, 1), (0, 1, 1)], (0, 1, 0), (150, 90, 60)),
    ([(0, 0, 0), (0, 1, 0), (0, 1, 1), (0, 0, 1)], (-1, 0, 0), (150, 90, 60)),
]
for verts, normal, color in faces:
    centre = tuple(sum(V[v][i] for v in verts) / 4 for i in range(3))
    if dot(normal, sub(LOCATION, centre)) <= 0:
        continue
    poly = [to_px(V[v]) for v in verts]
    d.polygon(poly, fill=color, outline=(40, 30, 25))
    for k in range(4):
        d.line([poly[k], poly[(k + 1) % 4]], fill=(40, 30, 25), width=3)

out = os.path.join(HERE, 'data')
img.save(os.path.join(out, 'synthetic_box.png'))
truth = dict(width=W, height=H, location=LOCATION, view_rows=VIEW, focal_rel=F,
             lens_mm_sensor36=F * 18.0, box_size=(BX, BY, BZ),
             box_corners_rel={','.join(map(str, k)): (lambda q: (q[0] / W, q[1] / H))(to_px(v))
                              for k, v in V.items()})
with open(os.path.join(out, 'synthetic_box.json'), 'w') as fh:
    json.dump(truth, fh, indent=1)
print('written', truth['lens_mm_sensor36'])
