# SPDX-License-Identifier: GPL-3.0-or-later
"""Detect Lines on the York Urban Database (Denis, Elder, Estrada, ECCV 2008):
102 photos 640x480 of indoor and outdoor scenes with a calibrated camera and
the true three directions (Manhattan frame) of each photo.

Not part of the add-on and not in the repository (the database is not ours):
download it from
https://www.elderlab.yorku.ca/resources/york-urban-line-segment-database-information/
and unzip into tests/YorkUrbanDB. Needs scipy (to read the .mat files) and
Pillow; runs in Claude's cloud workspace.

Run:  python3 tests/eval_york.py [path to YorkUrbanDB] [path to detect.py folder]
Prints, for every photo, the mode chosen and the errors, then a summary:
  direction error  largest angle between a true direction and the closest
                   found direction (degrees; signs and order do not matter)
  focal error      found focal length against the calibrated one (%)
"""
import glob
import math
import os
import sys
import time

import numpy as np
import scipy.io
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
DB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, 'YorkUrbanDB')
CODE = sys.argv[2] if len(sys.argv) > 2 else os.path.join(HERE, '..', 'perspective_match')
sys.path.insert(0, CODE)
import detect as D  # noqa: E402
import model as M   # noqa: E402


def mat_arrays(path):
    return {k: v for k, v in scipy.io.loadmat(path).items() if not k.startswith('__')}


def find(d, *names):
    for n in names:
        for k, v in d.items():
            if k.lower() == n.lower():
                return np.asarray(v, float)
    return None


def camera_parameters():
    cam = mat_arrays(glob.glob(os.path.join(DB, '**', 'cameraParameters.mat'), recursive=True)[0])
    focal, pixel = find(cam, 'focal').item(), find(cam, 'pixelSize').item()
    pp = find(cam, 'pp').ravel()
    return focal / pixel, pp


def true_directions(folder, name):
    files = glob.glob(os.path.join(folder, name + '*GroundTruthVP*CamParams*.mat'))
    if not files:
        return None
    d = mat_arrays(files[0])
    vp = find(d, 'vp_orthogonal', 'vp')
    if vp is None or vp.shape != (3, 3):
        return None
    return vp / np.linalg.norm(vp, axis=0)          # columns: directions, camera frame


def direction_error(R, truth):
    """Largest angle (degrees) between a true direction and the closest found
    one. R: world -> camera (camera y up, looking along -z); the database's
    directions have y up and z forward (checked: their vanishing points match
    the ones found on the photos only this way)."""
    found = [np.array([c[0], c[1], -c[2]]) for c in np.asarray(R).T]
    worst = 0.0
    for t in truth.T:
        best = min(math.degrees(math.acos(min(1.0, abs(float(t @ f))))) for f in found)
        worst = max(worst, best)
    return worst


def main():
    f_px, pp_px = camera_parameters()
    rows = []
    for folder in sorted(glob.glob(os.path.join(DB, '*'))):
        name = os.path.basename(folder)
        jpg = os.path.join(folder, name + '.jpg')
        if not os.path.exists(jpg):
            continue
        truth = true_directions(folder, name)
        if truth is None:
            continue
        a = np.asarray(Image.open(jpg).convert('RGB'))
        h, w = a.shape[:2]
        t0 = time.perf_counter()
        det = D.detect(a)
        ms = 1000.0 * (time.perf_counter() - t0)
        row = dict(name=name, mode=det.mode if det.ok else None, reason=det.reason, ms=ms)
        if det.ok:
            cur = M.CamState(aspect=w / h, f=f_px / (max(w, h) / 2.0), pp=(0.0, 0.0),
                             R=((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), (0.0, -1.0, 0.0)),
                             C=(0.0, -10.0, 1.6))
            p = D.plan(det, cur)
            if p is None:
                row['mode'] = None
                row['reason'] = 'plan failed'
            else:
                st = p[4]
                row['dir_err'] = direction_error(st.R, truth)
                if det.mode != 'ONE_VP':
                    row['f_err'] = 100.0 * abs(st.f * max(w, h) / 2.0 - f_px) / f_px
        rows.append(row)
        print(f"{name:12s} {str(row['mode']):10s} dir {row.get('dir_err', float('nan')):6.2f} deg  "
              f"f {row.get('f_err', float('nan')):6.1f} %  {ms:5.0f} ms  {row['reason']}")
    done = [r for r in rows if r['mode']]
    errs = np.array([r['dir_err'] for r in done])
    ferrs = np.array([r['f_err'] for r in done if 'f_err' in r])
    print()
    print(f'photos {len(rows)}, answered {len(done)}, refused {len(rows) - len(done)}')
    if len(errs):
        for t in (1, 2, 5, 10):
            print(f'  direction error <= {t:2d} deg: {np.mean(errs <= t) * 100:5.1f} % of answered, '
                  f'{np.sum(errs <= t) / len(rows) * 100:5.1f} % of all')
        print(f'  median direction error {np.median(errs):.2f} deg, worst {errs.max():.1f} deg')
    if len(ferrs):
        print(f'  focal error: median {np.median(ferrs):.1f} %, within 5 %: {np.mean(ferrs <= 5) * 100:.0f} %, '
              f'within 10 %: {np.mean(ferrs <= 10) * 100:.0f} %')
    modes = {}
    for r in rows:
        modes[r['mode']] = modes.get(r['mode'], 0) + 1
    print('  modes', modes)
    print(f"  time: median {np.median([r['ms'] for r in rows]):.0f} ms")


if __name__ == '__main__':
    main()
