# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic "photos" with a known camera for tests/test_detect.py: buildings,
rooms and a corridor drawn with Pillow (supersampled, slightly blurred and
noisy), plus pictures without straight edges. Runs in Claude's cloud
workspace (needs Pillow), not in Blender.

Writes tests/data/detect/<name>.png and <name>.json (camera, expected mode).
"""
import json
import math
import os
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'data', 'detect')
SS = 2                                   # supersampling


def norm(v):
    v = np.asarray(v, float)
    return v / np.linalg.norm(v)


class Camera:
    """Look-at camera with roll, relative focal length f (longer image side
    spans [-1, 1] on the image plane, as solver.py) and principal point pp."""

    def __init__(self, loc, target, f, w, h, roll=0.0, pp=(0.0, 0.0), level=False):
        self.C = np.asarray(loc, float)
        fwd = norm(np.subtract(target, loc))
        if level:
            fwd = norm((fwd[0], fwd[1], 0.0))
        right = norm(np.cross(fwd, (0, 0, 1)))
        up = np.cross(right, fwd)
        c, s = math.cos(roll), math.sin(roll)
        x, y = right * c + up * s, -right * s + up * c
        self.R = np.array([x, y, -fwd])         # rows: world -> camera
        self.f, self.w, self.h, self.pp = f, w, h, np.asarray(pp, float)
        self.half = max(w, h) / 2.0

    def cam(self, p):
        return self.R @ (np.asarray(p, float) - self.C)

    def px(self, c):
        """Camera-space point (in front) -> pixel (x right, y down)."""
        u = self.pp[0] + self.f * c[0] / -c[2]
        v = self.pp[1] + self.f * c[1] / -c[2]
        return (self.w / 2 + u * self.half, self.h / 2 - v * self.half)

    def truth(self):
        return dict(R=self.R.tolist(), C=self.C.tolist(), f=self.f, pp=self.pp.tolist(),
                    width=self.w, height=self.h)


NEAR = 0.05


def clip_poly(cam, pts):
    """Polygon (world) clipped to z < -NEAR in camera space, in pixels."""
    cs = [cam.cam(p) for p in pts]
    out = []
    for i in range(len(cs)):
        a, b = cs[i], cs[(i + 1) % len(cs)]
        ina, inb = a[2] < -NEAR, b[2] < -NEAR
        if ina:
            out.append(a)
        if ina != inb:
            t = (-NEAR - a[2]) / (b[2] - a[2])
            out.append(a + t * (b - a))
    return [cam.px(c) for c in out]


def depth(cam, pts):
    return float(np.mean([-cam.cam(p)[2] for p in pts]))


def box(x0, y0, z0, x1, y1, z1):
    """Faces of an axis-aligned box: (corner list, outward normal)."""
    return [
        ([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)], (0, -1, 0)),
        ([(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)], (1, 0, 0)),
        ([(x1, y1, z0), (x0, y1, z0), (x0, y1, z1), (x1, y1, z1)], (0, 1, 0)),
        ([(x0, y1, z0), (x0, y0, z0), (x0, y0, z1), (x0, y1, z1)], (-1, 0, 0)),
        ([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], (0, 0, 1)),
        ([(x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0)], (0, 0, -1)),
    ]


def windows(face, rows, cols, margin=0.12):
    """Window rectangles on a box face (corners a, b, c, d: bottom edge a-b)."""
    a, b, c, d = (np.asarray(p, float) for p in face)
    u, v = b - a, d - a
    out = []
    for i in range(cols):
        for j in range(rows):
            s0, s1 = (i + margin) / cols, (i + 1 - margin) / cols
            t0, t1 = (j + 1.5 * margin) / rows, (j + 1 - margin) / rows
            out.append([a + s0 * u + t0 * v, a + s1 * u + t0 * v,
                        a + s1 * u + t1 * v, a + s0 * u + t1 * v])
    return out


def render(cam, polys, sky=(178, 196, 214), ground=(140, 132, 120), noise=3.0, seed=0):
    """polys: (world corners, fill, outline or None[, corners for the depth]).
    Painter's order (stable: windows stay after their wall)."""
    W, H = cam.w * SS, cam.h * SS
    img = Image.new('RGB', (W, H), sky)
    d = ImageDraw.Draw(img)
    # ground plane: a huge quad at z = 0
    g = clip_poly(cam, [(-1e4, -1e4, 0), (1e4, -1e4, 0), (1e4, 1e4, 0), (-1e4, 1e4, 0)])
    if len(g) >= 3:
        d.polygon([(x * SS, y * SS) for x, y in g], fill=ground)
    for item in sorted(polys, key=lambda q: -depth(cam, q[3] if len(q) > 3 else q[0])):
        pts, fill, outline = item[:3]
        p = clip_poly(cam, pts)
        if len(p) < 3:
            continue
        d.polygon([(x * SS, y * SS) for x, y in p], fill=fill,
                  outline=None)
        if outline is not None:
            d.line([(x * SS, y * SS) for x, y in p + [p[0]]], fill=outline, width=2 * SS)
    img = img.resize((cam.w, cam.h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(0.6))
    a = np.asarray(img, np.float32)
    a += np.random.default_rng(seed).normal(0.0, noise, a.shape).astype(np.float32)
    return Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))


def building(cam, x0, y0, x1, y1, height, color, rows, cols_x, cols_y, rng):
    polys = []
    for face, n in box(x0, y0, 0.0, x1, y1, height)[:5]:
        centre = np.mean(face, axis=0)
        if np.dot(n, cam.C - centre) <= 0:
            continue
        shade = 1.0 - 0.18 * abs(n[0]) - 0.08 * abs(n[2])
        fc = tuple(int(c * shade) for c in color)
        polys.append((face, fc, (60, 55, 50)))
        if n[2] == 0:
            cols = cols_x if n[1] != 0 else cols_y
            for wnd in windows(face, rows, cols):
                tint = rng.integers(40, 90)
                polys.append(([tuple(p + 0.01 * np.asarray(n)) for p in wnd],
                              (tint, tint + 10, tint + 25), None, face))
    return polys


def room(cam, sx, sy, sz, rng, tiles=1.0):
    """Inside of a box room (0..sx, 0..sy, 0..sz): floor tiles, walls with
    doors / pictures, ceiling panels."""
    polys = []
    for i in np.arange(0, sx, tiles):
        for j in np.arange(0, sy, tiles):
            c = 200 if (int(i / tiles) + int(j / tiles)) % 2 else 170
            polys.append(([(i, j, 0), (i + tiles, j, 0), (i + tiles, j + tiles, 0), (i, j + tiles, 0)],
                          (c, c - 10, c - 25), None))
    walls = [
        ([(0, sy, 0), (sx, sy, 0), (sx, sy, sz), (0, sy, sz)], (225, 220, 205)),
        ([(sx, 0, 0), (sx, sy, 0), (sx, sy, sz), (sx, 0, sz)], (210, 205, 195)),
        ([(0, 0, 0), (0, sy, 0), (0, sy, sz), (0, 0, sz)], (215, 210, 200)),
        ([(0, 0, 0), (sx, 0, 0), (sx, 0, sz), (0, 0, sz)], (220, 215, 200)),
    ]
    for face, col in walls:
        polys.append((face, col, (120, 110, 100)))
        for wnd in windows(face, 2, int(rng.integers(2, 4)), margin=0.25):
            polys.append((wnd, (90, 110, 140), (40, 40, 40), face))
    polys.append(([(0, 0, sz), (sx, 0, sz), (sx, sy, sz), (0, sy, sz)], (240, 240, 235), None))
    return polys


def corridor(cam, length, width, height):
    polys = [([(0, 0, 0), (width, 0, 0), (width, length, 0), (0, length, 0)], (150, 140, 130), None)]
    for x in (0.0, width):
        polys.append(([(x, 0, 0), (x, length, 0), (x, length, height), (x, 0, height)],
                      (215, 210, 200), None))
    polys.append(([(0, 0, height), (width, 0, height), (width, length, height), (0, length, height)],
                  (235, 235, 230), None))
    for y in np.arange(1.0, length, 2.0):          # doors and frames along the walls
        for x, s in ((0.0, 1), (width, -1)):
            polys.append(([(x + s * 0.01, y, 0), (x + s * 0.01, y + 0.9, 0),
                           (x + s * 0.01, y + 0.9, 2.1), (x + s * 0.01, y, 2.1)],
                          (120, 85, 60), (50, 35, 25)))
    for y in np.arange(0.0, length, 1.5):          # ceiling lights across
        polys.append(([(0.6, y, height - 0.01), (width - 0.6, y, height - 0.01),
                       (width - 0.6, y + 0.3, height - 0.01), (0.6, y + 0.3, height - 0.01)],
                      (255, 255, 245), None))
    for y in np.arange(0.0, length, 0.6):          # floor tile joints across
        polys.append(([(0, y, 0.002), (width, y, 0.002), (width, y + 0.03, 0.002), (0, y + 0.03, 0.002)],
                      (110, 100, 95), None))
    polys.append(([(0, length, 0), (width, length, 0), (width, length, height), (0, length, height)],
                  (190, 200, 210), (60, 60, 60)))
    return polys


def street(cam, rng):
    polys = []
    for k in range(4):
        y0 = k * 14.0
        polys += building(cam, 0.0, y0, 10.0, y0 + 12.0, rng.uniform(12, 25), (190, 175, 150),
                          int(rng.integers(4, 7)), 5, 4, rng)
    return polys


def save(name, img, cam, mode, axes, note=''):
    os.makedirs(OUT, exist_ok=True)
    img.save(os.path.join(OUT, name + '.png'))
    truth = cam.truth() if cam is not None else None
    with open(os.path.join(OUT, name + '.json'), 'w') as fh:
        json.dump(dict(camera=truth, mode=mode, axes=axes, note=note), fh, indent=1)
    print('written', name, mode)


def main():
    rng = np.random.default_rng(5)
    W, H = 1600, 1067
    # 2 Points: corner of a building, camera tilted up a little (verticals converge
    # slightly), principal point at the centre -> 2 Points (X, Y)
    cam = Camera((26.0, -22.0, 1.7), (2.0, 4.0, 7.0), 1.3, W, H)
    polys = building(cam, 0.0, 0.0, 14.0, 10.0, 18.0, (200, 180, 150), 6, 7, 5, rng)
    save('2p_building', render(cam, polys, seed=1), cam, 'TWO_VP', ['X', 'Y'])
    # Parallel Verticals: the same, camera level (shift up to frame the building)
    cam = Camera((26.0, -22.0, 1.7), (2.0, 4.0, 7.0), 1.3, W, H, pp=(0.0, -0.25), level=True)
    save('pv_building', render(cam, polys, seed=2), cam, 'VERTICALS', ['X', 'Y'])
    # 3 Points: strong tilt up at the corner and a shifted principal point
    cam = Camera((25.0, -11.0, 1.6), (12.0, 1.0, 13.0), 0.9, W, H, pp=(0.3, 0.25))
    polys = building(cam, 0.0, 0.0, 14.0, 10.0, 30.0, (190, 185, 175), 9, 4, 3, rng)
    save('3p_tower_shift', render(cam, polys, seed=3), cam, 'THIRD_VP', ['X', 'Y'])
    # strong tilt, principal point at the centre -> 2 Points is the expected choice
    cam = Camera((25.0, -11.0, 1.6), (12.0, 1.0, 13.0), 0.9, W, H)
    save('3p_tower_centre', render(cam, polys, seed=4), cam, 'TWO_VP', ['X', 'Y'],
         note='three vanishing points, principal point at the centre: 2 Points expected')
    # interior: room seen from a corner, slight downward tilt, roll
    cam = Camera((0.8, 0.9, 1.5), (4.0, 5.0, 1.0), 0.9, W, H, roll=math.radians(2.0))
    polys = room(cam, 6.0, 7.0, 2.8, rng)
    save('2p_room', render(cam, polys, ground=(150, 140, 130), seed=5), cam, 'TWO_VP', ['X', 'Y'])
    # wide angle interior, level camera
    cam = Camera((0.7, 0.6, 1.4), (5.0, 6.0, 1.4), 0.55, W, H, level=True)
    save('pv_room_wide', render(cam, polys, ground=(150, 140, 130), seed=6), cam, 'VERTICALS',
         ['X', 'Y'])
    # 1 Point: straight down a corridor (level, looking along +Y)
    cam = Camera((1.2, -0.5, 1.6), (1.2, 30.0, 1.6), 1.1, W, H)
    polys = corridor(cam, 30.0, 2.4, 2.7)
    save('1p_corridor', render(cam, polys, seed=7), cam, 'ONE_VP', ['Y'])
    # street: buildings along a street, a little turned -> 2 Points with a far point
    cam = Camera((-6.0, -18.0, 1.7), (6.0, 30.0, 6.0), 1.4, W, H)
    save('2p_street', render(cam, street(cam, rng), seed=8), cam, 'TWO_VP', ['X', 'Y'],
         note='one horizontal vanishing point far away')
    # pictures without straight edges: honest refusal
    noise = np.random.default_rng(9).normal(128, 40, (H, W, 3))
    save('none_noise', Image.fromarray(np.clip(noise, 0, 255).astype(np.uint8)), None, None, [])
    yy, xx = np.mgrid[0:H, 0:W]
    grad = np.stack([xx / W * 255, yy / H * 255, (xx + yy) / (W + H) * 255], -1)
    save('none_gradient', Image.fromarray(grad.astype(np.uint8)), None, None, [])
    blobs = Image.new('RGB', (W, H), (70, 110, 60))
    d = ImageDraw.Draw(blobs)
    r = random.Random(3)
    for _ in range(400):
        x, y, s = r.uniform(0, W), r.uniform(0, H), r.uniform(10, 90)
        c = (r.randint(30, 120), r.randint(80, 170), r.randint(20, 90))
        d.ellipse([x - s, y - s * r.uniform(0.5, 1.5), x + s, y + s], fill=c)
    save('none_blobs', blobs.filter(ImageFilter.GaussianBlur(2.0)), None, None, [])


if __name__ == '__main__':
    main()
