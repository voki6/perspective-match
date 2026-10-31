# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic ground-truth tests for solver.py (no Blender needed).

A random camera is generated, edges parallel to the world axes are projected
into the image, and the solver must recover the same camera.

Run:  python3 tests/test_solver.py      (or: python3 -m pytest tests)
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'perspective_match'))
import solver as S  # noqa: E402

ASPECTS = [1.5, 16 / 9, 1.0, 2 / 3, 9 / 16, 4 / 3, 3 / 4]
WORLD_AXES = {'X': (1.0, 0.0, 0.0), 'Y': (0.0, 1.0, 0.0), 'Z': (0.0, 0.0, 1.0)}


# ---------------------------------------------------------------- helpers

def norm(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v)


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def sub(a, b):
    return tuple(x - y for x, y in zip(a, b))


def add(a, b):
    return tuple(x + y for x, y in zip(a, b))


def mul(a, s):
    return tuple(x * s for x in a)


def matvec(m, v):
    return tuple(sum(m[i][j] * v[j] for j in range(3)) for i in range(3))


def mat_err(a, b):
    return max(abs(a[i][j] - b[i][j]) for i in range(3) for j in range(3))


class Camera:
    """Ground-truth pinhole camera in solver conventions."""

    def __init__(self, rng, aspect, f, pp=(0.0, 0.0), level=False, max_roll=0.3):
        self.aspect, self.f, self.pp = aspect, f, pp
        while True:
            yaw = rng.uniform(0.0, 2.0 * math.pi)
            dist = rng.uniform(5.0, 20.0)
            height = rng.uniform(0.5, 8.0)
            self.location = (dist * math.cos(yaw), dist * math.sin(yaw), height)
            target = (rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(0.0, 2.0))
            if level:
                target = (target[0], target[1], height)
            fwd = norm(sub(target, self.location))
            right = norm(cross(fwd, (0.0, 0.0, 1.0)))
            up = cross(right, fwd)
            roll = rng.uniform(-max_roll, max_roll)
            cr, sr = math.cos(roll), math.sin(roll)
            x = add(mul(right, cr), mul(up, sr))
            y = add(mul(right, -sr), mul(up, cr))
            z = mul(fwd, -1.0)
            # world -> camera: rows are the camera axes in world space
            self.view = (x, y, z)
            # every world axis must have a finite vanishing point (unless level)
            ok = all(abs(matvec(self.view, a)[2]) > 0.05 for a in WORLD_AXES.values())
            if level or ok:
                break

    def to_cam(self, p):
        return matvec(self.view, sub(p, self.location))

    def project(self, p):
        c = self.to_cam(p)
        assert c[2] < -1e-6, 'point behind camera'
        x = self.pp[0] + self.f * c[0] / -c[2]
        y = self.pp[1] + self.f * c[1] / -c[2]
        return S.plane_to_rel((x, y), self.aspect)

    def axis_label(self, name):
        d = matvec(self.view, WORLD_AXES[name])
        return ('+' if d[2] < 0 else '-') + name

    def segments(self, name, rng, count=2, noise=0.0):
        axis = WORLD_AXES[name]
        segs = []
        for _ in range(count):
            off = [rng.uniform(-1.5, 1.5) for _ in range(3)]
            off[list(WORLD_AXES).index(name)] = rng.uniform(-1.0, 0.0)
            p0 = tuple(off)
            p1 = add(p0, mul(axis, rng.uniform(1.0, 2.5)))
            a, b = self.project(p0), self.project(p1)
            if noise:
                a = (a[0] + rng.gauss(0, noise), a[1] + rng.gauss(0, noise))
                b = (b[0] + rng.gauss(0, noise), b[1] + rng.gauss(0, noise))
            segs.append((a, b))
        return segs


def check(sol, cam, loc_tol=1e-6, rot_tol=1e-9, f_tol=1e-9, check_location=True):
    assert sol.ok, (sol.errors, sol.warnings)
    assert abs(sol.focal_rel - cam.f) < f_tol, (sol.focal_rel, cam.f)
    assert mat_err(sol.view_rotation, cam.view) < rot_tol, mat_err(sol.view_rotation, cam.view)
    assert abs(sol.principal_point[0] - cam.pp[0]) < 1e-7
    assert abs(sol.principal_point[1] - cam.pp[1]) < 1e-7
    if check_location:
        err = max(abs(a - b) for a, b in zip(sol.camera_location, cam.location))
        assert err < loc_tol, (sol.camera_location, cam.location)


def height_settings(cam, **kw):
    return S.Settings(scale_mode='CAMERA_HEIGHT', reference_axis='Z',
                      camera_height=cam.location[2], **kw)


# ---------------------------------------------------------------- tests

def test_2vp_center_all_aspects():
    rng = random.Random(1)
    for i in range(300):
        aspect = ASPECTS[i % len(ASPECTS)]
        cam = Camera(rng, aspect, rng.uniform(0.6, 4.0))
        a1, a2 = rng.sample(['X', 'Y', 'Z'], 2)
        pts = S.ControlPoints(vp1=cam.segments(a1, rng), vp2=cam.segments(a2, rng),
                              origin=cam.project((0, 0, 0)))
        st = height_settings(cam, vp1_axis=cam.axis_label(a1), vp2_axis=cam.axis_label(a2))
        check(S.solve(pts, st, aspect), cam)


def test_2vp_manual_principal_point():
    rng = random.Random(2)
    for i in range(300):
        aspect = ASPECTS[i % len(ASPECTS)]
        pp = (rng.uniform(-0.3, 0.3), rng.uniform(-0.3, 0.3))
        cam = Camera(rng, aspect, rng.uniform(0.6, 4.0), pp)
        pts = S.ControlPoints(vp1=cam.segments('X', rng), vp2=cam.segments('Y', rng),
                              origin=cam.project((0, 0, 0)),
                              principal_point=S.plane_to_rel(pp, aspect))
        st = height_settings(cam, pp_mode='MANUAL',
                             vp1_axis=cam.axis_label('X'), vp2_axis=cam.axis_label('Y'))
        check(S.solve(pts, st, aspect), cam)


def test_2vp_principal_point_from_third_vp():
    rng = random.Random(3)
    for i in range(300):
        aspect = ASPECTS[i % len(ASPECTS)]
        pp = (rng.uniform(-0.3, 0.3), rng.uniform(-0.3, 0.3))
        cam = Camera(rng, aspect, rng.uniform(0.6, 2.5), pp)
        pts = S.ControlPoints(vp1=cam.segments('X', rng), vp2=cam.segments('Y', rng),
                              vp3=cam.segments('Z', rng), origin=cam.project((0, 0, 0)))
        st = height_settings(cam, pp_mode='THIRD_VP',
                             vp1_axis=cam.axis_label('X'), vp2_axis=cam.axis_label('Y'))
        # orthocentre of far-away vanishing points loses some precision
        check(S.solve(pts, st, aspect), cam, loc_tol=1e-4, rot_tol=1e-6, f_tol=1e-6)


def test_2vp_parallel_verticals():
    rng = random.Random(4)
    for i in range(300):
        aspect = ASPECTS[i % len(ASPECTS)]
        pp = (rng.uniform(-0.3, 0.3), rng.uniform(-0.5, 0.5))   # lens shift
        cam = Camera(rng, aspect, rng.uniform(0.6, 4.0), pp, level=True)
        # the control point may sit anywhere on the perpendicular through pp
        vp_x = S.project_direction(cam.view, (1, 0, 0), cam.f, pp)
        vp_y = S.project_direction(cam.view, (0, 1, 0), cam.f, pp)
        if vp_x is None or vp_y is None:
            continue
        d = norm((vp_y[0] - vp_x[0], vp_y[1] - vp_x[1], 0.0))
        nrm = (-d[1], d[0])
        k = rng.uniform(-0.4, 0.4)
        control = (pp[0] + k * nrm[0], pp[1] + k * nrm[1])
        pts = S.ControlPoints(vp1=cam.segments('X', rng), vp2=cam.segments('Y', rng),
                              origin=cam.project((0, 0, 0)),
                              principal_point=S.plane_to_rel(control, aspect))
        st = height_settings(cam, pp_mode='VERTICALS',
                             vp1_axis=cam.axis_label('X'), vp2_axis=cam.axis_label('Y'))
        sol = S.solve(pts, st, aspect)
        check(sol, cam, loc_tol=1e-5, rot_tol=1e-8, f_tol=1e-8)
        assert sol.vanishing_points[2] is None          # verticals stay parallel
        forward = S._matvec3(sol.camera_rotation, (0.0, 0.0, -1.0))
        assert abs(forward[2]) < 1e-9                    # no pitch


def test_1vp():
    rng = random.Random(5)
    for i in range(300):
        aspect = ASPECTS[i % len(ASPECTS)]
        manual = i % 2 == 1
        pp = (rng.uniform(-0.3, 0.3), rng.uniform(-0.3, 0.3)) if manual else (0.0, 0.0)
        cam = Camera(rng, aspect, rng.uniform(0.6, 4.0), pp)
        a1, a2 = rng.sample(['X', 'Y', 'Z'], 2)
        v1 = S.project_direction(cam.view, WORLD_AXES[a1], cam.f, pp)
        v2 = S.project_direction(cam.view, WORLD_AXES[a2], cam.f, pp)
        # the "horizon" control only gives the direction of the VP1-VP2 line
        h0 = (rng.uniform(0.2, 0.8), rng.uniform(0.2, 0.8))
        d = (v2[0] - v1[0], v2[1] - v1[1])
        h0p = S.rel_to_plane(h0, aspect)
        h1 = S.plane_to_rel((h0p[0] + 0.3 * d[0] / math.hypot(*d),
                             h0p[1] + 0.3 * d[1] / math.hypot(*d)), aspect)
        pts = S.ControlPoints(vp1=cam.segments(a1, rng), horizon=(h0, h1),
                              origin=cam.project((0, 0, 0)),
                              principal_point=S.plane_to_rel(pp, aspect))
        st = height_settings(cam, mode='1VP', focal_rel=cam.f,
                             pp_mode='MANUAL' if manual else 'CENTER',
                             vp1_axis=cam.axis_label(a1), vp2_axis=cam.axis_label(a2))
        check(S.solve(pts, st, aspect), cam, loc_tol=1e-5, rot_tol=1e-8)


def test_1vp_parallel_verticals():
    rng = random.Random(6)
    for i in range(300):
        aspect = ASPECTS[i % len(ASPECTS)]
        pp = (rng.uniform(-0.2, 0.2), rng.uniform(-0.5, 0.5))
        cam = Camera(rng, aspect, rng.uniform(0.6, 4.0), pp, level=True)
        v1 = S.project_direction(cam.view, (1, 0, 0), cam.f, pp)
        v2 = S.project_direction(cam.view, (0, 1, 0), cam.f, pp)
        if v1 is None or v2 is None:
            continue
        d = norm((v2[0] - v1[0], v2[1] - v1[1], 0.0))
        control = (pp[0] - 0.3 * d[1], pp[1] + 0.3 * d[0])   # off the horizon
        h0 = (0.3, 0.7)
        h0p = S.rel_to_plane(h0, aspect)
        h1 = S.plane_to_rel((h0p[0] + 0.4 * d[0], h0p[1] + 0.4 * d[1]), aspect)
        pts = S.ControlPoints(vp1=cam.segments('X', rng), horizon=(h0, h1),
                              origin=cam.project((0, 0, 0)),
                              principal_point=S.plane_to_rel(control, aspect))
        st = height_settings(cam, mode='1VP', focal_rel=cam.f, pp_mode='VERTICALS',
                             vp1_axis=cam.axis_label('X'), vp2_axis=cam.axis_label('Y'))
        check(S.solve(pts, st, aspect), cam, loc_tol=1e-5, rot_tol=1e-8)


def test_reference_distance():
    rng = random.Random(7)
    for i in range(200):
        aspect = ASPECTS[i % len(ASPECTS)]
        cam = Camera(rng, aspect, rng.uniform(0.6, 3.0))
        base = dict(vp1=cam.segments('X', rng), vp2=cam.segments('Y', rng),
                    origin=cam.project((0, 0, 0)))
        lab = dict(vp1_axis=cam.axis_label('X'), vp2_axis=cam.axis_label('Y'))
        # along Z through the origin, fSpy style
        pts = S.ControlPoints(reference=(cam.project((0, 0, 0.2)), cam.project((0, 0, 1.9))), **base)
        st = S.Settings(scale_mode='DISTANCE', reference_axis='Z', reference_distance=1.7, **lab)
        check(S.solve(pts, st, aspect), cam, loc_tol=1e-6)
        # along X through the origin
        pts = S.ControlPoints(reference=(cam.project((0, 0, 0)), cam.project((1.5, 0, 0))), **base)
        st = S.Settings(scale_mode='DISTANCE', reference_axis='X', reference_distance=1.5, **lab)
        check(S.solve(pts, st, aspect), cam, loc_tol=1e-6)
        # vertical distance at another spot on the ground (anchor)
        g = (rng.uniform(-1.5, 1.5), rng.uniform(-1.5, 1.5), 0.0)
        pts = S.ControlPoints(reference=(cam.project(add(g, (0, 0, 0.3))),
                                         cam.project(add(g, (0, 0, 2.4)))),
                              reference_anchor=cam.project(g), **base)
        st = S.Settings(scale_mode='DISTANCE', reference_axis='Z', reference_distance=2.1, **lab)
        check(S.solve(pts, st, aspect), cam, loc_tol=1e-6)


def test_many_lines_exact_and_noisy():
    """Exact data with many lines must be exact. With noisy endpoints, 8 lines
    per vanishing point must be clearly better than 2 (median optical axis
    error). Returns the median error in degrees for 8 lines."""
    rng = random.Random(8)
    errs = {2: [], 8: []}
    for i in range(200):
        aspect = ASPECTS[i % len(ASPECTS)]
        cam = Camera(rng, aspect, rng.uniform(0.8, 2.5))
        lab = dict(vp1_axis=cam.axis_label('X'), vp2_axis=cam.axis_label('Y'))
        pts = S.ControlPoints(vp1=cam.segments('X', rng, 6), vp2=cam.segments('Y', rng, 5),
                              origin=cam.project((0, 0, 0)))
        check(S.solve(pts, height_settings(cam, **lab), aspect), cam,
              loc_tol=1e-6, rot_tol=1e-8, f_tol=1e-8)
        # 0.05 % of the image size noise per endpoint (~2-3 px on a photo)
        for n in (2, 8):
            pts = S.ControlPoints(vp1=cam.segments('X', rng, n, 5e-4),
                                  vp2=cam.segments('Y', rng, n, 5e-4),
                                  origin=cam.project((0, 0, 0)))
            sol = S.solve(pts, height_settings(cam, **lab), aspect)
            if not sol.ok:
                errs[n].append(180.0)   # noise made the configuration impossible
                continue
            fa = S._matvec3(sol.camera_rotation, (0, 0, -1))
            fb = matvec(tuple(zip(*cam.view)), (0, 0, -1))
            cos = max(-1.0, min(1.0, sum(a * b for a, b in zip(fa, fb))))
            errs[n].append(math.degrees(math.acos(cos)))
    med = {n: sorted(v)[len(v) // 2] for n, v in errs.items()}
    assert med[8] < 1.5 and med[8] < 0.5 * med[2], med
    return med[8]


def test_errors_do_not_raise():
    cases = [
        S.ControlPoints(vp1=[((0.1, 0.1), (0.9, 0.1)), ((0.1, 0.2), (0.9, 0.2))],
                        vp2=[((0.1, 0.1), (0.1, 0.9)), ((0.2, 0.1), (0.3, 0.9))]),   # VP1 parallel
        S.ControlPoints(vp1=[((0.1, 0.1), (0.1, 0.1)), ((0.1, 0.2), (0.9, 0.2))]),  # zero length
        S.ControlPoints(vp1=[((0.0, 0.4), (0.4, 0.5)), ((0.0, 0.6), (0.4, 0.55))],
                        vp2=[((0.1, 0.4), (0.5, 0.48)), ((0.1, 0.6), (0.5, 0.52))]),  # same side
        S.ControlPoints(),                                                          # no lines
    ]
    for i, pts in enumerate(cases):
        settings = [S.Settings(), S.Settings(pp_mode='VERTICALS'), S.Settings(pp_mode='THIRD_VP'),
                    S.Settings(vp1_axis='+X', vp2_axis='-X')]
        if i != 2:   # case 2 has a valid VP1, so 1VP mode is legitimately solvable
            settings.append(S.Settings(mode='1VP'))
        for st in settings:
            sol = S.solve(pts, st, 1.5)
            assert not sol.ok and sol.errors, (i, st)


def test_blender_mapping_roundtrip():
    sol = S.Solution(aspect=1.0, focal_rel=1.7, principal_point=(0.1, -0.2),
                     camera_rotation=((1, 0, 0), (0, 1, 0), (0, 0, 1)))
    for aspect in ASPECTS:
        sol.aspect = aspect
        for fit in ('AUTO', 'HORIZONTAL', 'VERTICAL'):
            cam = S.blender_camera_settings(sol, fit, 36.0, 24.0)
            f = S.focal_rel_from_blender(cam['lens'], aspect, fit, 36.0, 24.0)
            assert abs(f - sol.focal_rel) < 1e-12
    # AUTO on a wide image must equal the fSpy importer (FOV + shift = -pp/2)
    sol.aspect = 1.5
    cam = S.blender_camera_settings(sol, 'AUTO', 36.0, 24.0)
    assert abs(2 * math.atan(18.0 / cam['lens']) - 2 * math.atan(1 / 1.7)) < 1e-12
    assert abs(cam['shift_x'] + 0.05) < 1e-12 and abs(cam['shift_y'] - 0.1) < 1e-12


def _dist_to_line(q, p, d):
    return abs(d[0] * (q[1] - p[1]) - d[1] * (q[0] - p[0])) / math.hypot(d[0], d[1])


def test_horizon_any_axis_pair():
    """The horizon passes through the vanishing points of all horizontal
    directions, whichever axes the two vanishing points use (also X + Z)."""
    rng = random.Random(21)
    worst = 0.0
    for i in range(300):
        aspect = ASPECTS[i % len(ASPECTS)]
        pp = (rng.uniform(-0.2, 0.2), rng.uniform(-0.2, 0.2))
        cam = Camera(rng, aspect, rng.uniform(0.6, 4.0), pp)
        a1, a2 = [('X', 'Y'), ('X', 'Z'), ('Z', 'Y'), ('Y', 'Z')][i % 4]
        pts = S.ControlPoints(vp1=cam.segments(a1, rng), vp2=cam.segments(a2, rng),
                              origin=cam.project((0, 0, 0)),
                              principal_point=S.plane_to_rel(pp, aspect))
        st = S.Settings(pp_mode='MANUAL', vp1_axis=cam.axis_label(a1), vp2_axis=cam.axis_label(a2))
        sol = S.solve(pts, st, aspect)
        assert sol.ok, sol.errors
        p, d = sol.horizon()
        for ang in (0.0, 0.7, 1.9, 2.8):
            direction = (math.cos(ang), math.sin(ang), 0.0)
            vp = S.project_direction(sol.view_rotation, direction, sol.focal_rel, sol.principal_point)
            if vp is not None and math.hypot(*vp) < 50:
                worst = max(worst, _dist_to_line(vp, p, d))
    assert worst < 1e-9, worst


def test_horizon_absent_looking_straight_down():
    sol = S.Solution(ok=True, aspect=1.5, focal_rel=1.2, principal_point=(0.0, 0.0),
                     view_rotation=((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
    assert sol.horizon() is None and sol.horizon_rel() is None


def test_jacobi_eigenvector():
    rng = random.Random(9)
    for _ in range(200):
        a = [[rng.uniform(-1, 1) for _ in range(3)] for _ in range(3)]
        m = [[sum(a[k][i] * a[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
        v = S._sym3_smallest_eigenvector(m)
        mv = [sum(m[i][j] * v[j] for j in range(3)) for i in range(3)]
        lam = sum(mv[i] * v[i] for i in range(3))
        assert max(abs(mv[i] - lam * v[i]) for i in range(3)) < 1e-9
        # smallest: Rayleigh quotient of random unit vectors is never below it
        for _ in range(20):
            u = norm([rng.gauss(0, 1) for _ in range(3)])
            q = sum(u[i] * sum(m[i][j] * u[j] for j in range(3)) for i in range(3))
            assert q >= lam - 1e-12


if __name__ == '__main__':
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                out = fn()
                print(f'PASS {name}' + (f'  ({out:.3f})' if isinstance(out, float) else ''))
            except AssertionError as e:
                failed += 1
                print(f'FAIL {name}: {e!r}')
    sys.exit(1 if failed else 0)
