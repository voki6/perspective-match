# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for detect.py on the synthetic photos of make_detect_photos.py (run
that first). Needs Pillow to read the PNG files (Claude's cloud workspace).

Run:  python3 tests/test_detect.py
"""
import glob
import json
import math
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'perspective_match'))
import detect as D  # noqa: E402
import model as M   # noqa: E402

DATA = os.path.join(HERE, 'data', 'detect')


def rot_angle(a, b):
    """Angle (degrees) of the rotation between two world -> camera matrices."""
    r = np.asarray(a) @ np.asarray(b).T
    return math.degrees(math.acos(max(-1.0, min(1.0, (np.trace(r) - 1.0) / 2.0))))


def yaw(R, deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    rz = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    return np.asarray(R) @ rz.T


def run(name):
    truth = json.load(open(os.path.join(DATA, name + '.json')))
    pixels = np.asarray(Image.open(os.path.join(DATA, name + '.png')).convert('RGB'))
    t0 = time.perf_counter()
    det = D.detect(pixels)
    ms = 1000.0 * (time.perf_counter() - t0)
    out = dict(name=name, expected=truth['mode'], mode=det.mode if det.ok else None,
               reason=det.reason, ms=round(ms))
    cam = truth['camera']
    if det.ok and cam:
        w, h = cam['width'], cam['height']
        # the user's camera before pressing: the true one turned by 15 degrees,
        # with the true focal length (1 Point keeps it)
        cur = M.CamState(aspect=w / h, f=cam['f'], pp=(0.0, 0.0),
                         R=tuple(map(tuple, yaw(cam['R'], 15.0))), C=tuple(cam['C']))
        if det.mode == 'VERTICALS':
            cur.pp = (0.0, cam['pp'][1])
        p = D.plan(det, cur)
        if p is not None:
            st = p[4]
            out.update(axes=(p[1], p[2]), rot_err=round(rot_angle(st.R, cam['R']), 3),
                       f_err=round(100.0 * abs(st.f - cam['f']) / cam['f'], 2))
    return out


def test_synthetic_photos():
    rows = [run(os.path.basename(p)[:-4]) for p in sorted(glob.glob(os.path.join(DATA, '*.png')))]
    for r in rows:
        print(r)
    for r in rows:
        assert r['mode'] == r['expected'], r
        if r['expected'] is not None:
            assert r.get('rot_err', 99) < 0.5, r
            assert r['mode'] == 'ONE_VP' or r.get('f_err', 99) < 2.0, r


if __name__ == '__main__':
    test_synthetic_photos()
    print('PASS')
