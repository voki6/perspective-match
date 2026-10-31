# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for model.py: camera -> lines -> camera without Blender.

Run:  python3 tests/test_model.py
"""

import math
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'perspective_match'))
import model as M   # noqa: E402
import solver as S  # noqa: E402

ASPECTS = [1.5, 16 / 9, 1.0, 2 / 3, 9 / 16, 4 / 3]
PAIRS = [('X', 'Y'), ('Y', 'X'), ('X', 'Z'), ('Z', 'Y'), ('Y', 'Z')]


def norm(v):
    n = math.sqrt(sum(c * c for c in v))
    return tuple(c / n for c in v)


def cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def make_state(rng, aspect, level=False, pp=None):
    while True:
        yaw = rng.uniform(0, 2 * math.pi)
        dist = rng.uniform(4.0, 15.0)
        h = rng.uniform(0.5, 6.0)
        loc = (dist * math.cos(yaw), dist * math.sin(yaw), h)
        target = (rng.uniform(-1, 1), rng.uniform(-1, 1), rng.uniform(0.0, 1.5))
        fwd = norm(tuple(t - c for t, c in zip(target, loc)))
        if level:
            fwd = norm((fwd[0], fwd[1], 0.0))
        right = norm(cross(fwd, (0, 0, 1)))
        up = cross(right, fwd)
        roll = 0.0 if level else rng.uniform(-0.25, 0.25)
        cr, sr = math.cos(roll), math.sin(roll)
        x = tuple(r * cr + u * sr for r, u in zip(right, up))
        y = tuple(-r * sr + u * cr for r, u in zip(right, up))
        z = tuple(-c for c in fwd)
        R = (x, y, z)
        if pp is None:
            pp_ = (rng.uniform(-0.2, 0.2), rng.uniform(-0.2, 0.2))
        else:
            pp_ = pp
        st = M.CamState(aspect=aspect, f=rng.uniform(0.8, 3.0), pp=pp_, R=R, C=loc)
        # all axes must have a usable vanishing point unless level
        ok = all(abs(M._matvec(R, M.AXIS_VECTORS[a])[2]) > 0.08 for a in 'XYZ')
        if (level or ok) and M.origin_rel(st) is not None:
            return st


def pp_for(mode, rng):
    """A principal point the mode keeps as it is (see model._PP_MODE)."""
    if mode in ('TWO_VP', 'ONE_VP'):
        return (0.0, 0.0)
    if mode == 'VERTICALS':                     # level camera: on the horizon, any x
        return (rng.uniform(-0.2, 0.2), rng.uniform(-0.2, 0.2))
    return (rng.uniform(-0.2, 0.2), rng.uniform(-0.2, 0.2))


def rot_err(a, b):
    return max(abs(a[i][j] - b[i][j]) for i in range(3) for j in range(3))


def state_err(a, b):
    return max(rot_err(a.R, b.R), abs(a.f - b.f), abs(a.pp[0] - b.pp[0]), abs(a.pp[1] - b.pp[1]),
               max(abs(x - y) for x, y in zip(a.C, b.C)) / 10.0)


def lines_of(st, axes='XYZ'):
    return {a: M.build_segments(st, a) for a in axes}


def perturb(rng, st, level=False, mode='THIRD_VP'):
    """A different camera (rotation, focal, shift, position)."""
    ang = rng.uniform(-0.15, 0.15)
    c, s = math.cos(ang), math.sin(ang)
    rz = ((c, -s, 0.0), (s, c, 0.0), (0.0, 0.0, 1.0))           # yaw about world Z
    R = M._matmul(st.R, M._transpose(rz))
    if not level:
        b = rng.uniform(-0.1, 0.1)
        cb, sb = math.cos(b), math.sin(b)
        rx = ((1.0, 0.0, 0.0), (0.0, cb, -sb), (0.0, sb, cb))      # pitch in camera space
        R = M._matmul(rx, R)
    C = tuple(x + rng.uniform(-0.5, 0.5) for x in st.C[:2]) + (st.C[2] * rng.uniform(0.8, 1.2),)
    pp = (st.pp[0] + rng.uniform(-0.05, 0.05), st.pp[1] + rng.uniform(-0.05, 0.05))
    if mode in ('TWO_VP', 'ONE_VP'):
        pp = (0.0, 0.0)
    return M.CamState(aspect=st.aspect, f=st.f * rng.uniform(0.85, 1.2), pp=pp, R=R, C=C)


# ------------------------------------------------------------------ tests

def test_built_lines_reproduce_camera_all_modes():
    rng = random.Random(3)
    worst = 0.0
    for i in range(400):
        aspect = ASPECTS[i % len(ASPECTS)]
        mode = M.MODES[i % 4]
        level = mode == 'VERTICALS'
        st = make_state(rng, aspect, level=level, pp=pp_for(mode, rng))
        a1, a2 = ('X', 'Y') if level else PAIRS[i % len(PAIRS)]
        res = M.solve_lines(lines_of(st), M.origin_rel(st), mode, a1, a2, st)
        assert res.ok, (mode, res.errors)
        worst = max(worst, state_err(res.state, st))
    assert worst < 1e-8, worst


def test_no_jump_after_camera_change():
    """camera changed by hand -> lines re-aimed -> touching the lines gives
    back exactly that camera (first touch must not jump)."""
    rng = random.Random(5)
    worst = 0.0
    for i in range(400):
        aspect = ASPECTS[i % len(ASPECTS)]
        mode = M.MODES[i % 4]
        level = mode == 'VERTICALS'
        st0 = make_state(rng, aspect, level=level, pp=pp_for(mode, rng))
        lines = lines_of(st0)
        st1 = perturb(rng, st0, level=level, mode=mode)
        if M.origin_rel(st1) is None:
            continue
        lines1 = M.retarget(st1, lines)
        a1, a2 = ('X', 'Y') if level else PAIRS[i % len(PAIRS)]
        res = M.solve_lines(lines1, M.origin_rel(st1), mode, a1, a2, st1)
        assert res.ok, (mode, res.errors)
        worst = max(worst, state_err(res.state, st1))
    assert worst < 1e-8, worst


def test_retarget_keeps_middle_and_length():
    rng = random.Random(7)
    for i in range(200):
        aspect = ASPECTS[i % len(ASPECTS)]
        st = make_state(rng, aspect)
        seg = ((rng.uniform(0.1, 0.9), rng.uniform(0.1, 0.9)),
               (rng.uniform(0.1, 0.9), rng.uniform(0.1, 0.9)))
        for axis in 'XYZ':
            vp = st.vp_h(M.AXIS_VECTORS[axis])
            a, b = M.retarget_segment(seg, vp, aspect)
            pa, pb = S.rel_to_plane(a, aspect), S.rel_to_plane(b, aspect)
            qa, qb = S.rel_to_plane(seg[0], aspect), S.rel_to_plane(seg[1], aspect)
            assert abs(math.dist(pa, pb) - math.dist(qa, qb)) < 1e-12
            assert math.dist(((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2),
                             ((qa[0] + qb[0]) / 2, (qa[1] + qb[1]) / 2)) < 1e-12
            # the line passes through the vanishing point
            x, y, w = vp
            d = (pb[0] - pa[0], pb[1] - pa[1])
            dist = abs(d[0] * (y - pa[1] * w) - d[1] * (x - pa[0] * w)) / math.hypot(*d)
            assert dist < 1e-9 * max(1.0, abs(x), abs(y)), dist


def test_distance_is_kept_when_lines_move():
    """Moving the lines turns the camera; it keeps its distance to the world
    origin and still sees the origin at the origin point, so it never runs
    off, even with the origin near the horizon or far outside the photo."""
    rng = random.Random(9)
    checked = 0
    for i in range(400):
        st = make_state(rng, ASPECTS[i % len(ASPECTS)], pp=(0.0, 0.0))
        if i % 2:                    # turn away: the origin far outside the photo, side-on
            ang = math.radians(rng.choice((-1, 1)) * rng.uniform(70.0, 89.5))
            c, s_ = math.cos(ang), math.sin(ang)
            rz = ((c, -s_, 0.0), (s_, c, 0.0), (0.0, 0.0, 1.0))
            st = M.CamState(st.aspect, st.f, st.pp, M._matmul(st.R, M._transpose(rz)), st.C)
            if M.origin_rel(st) is None or not all(
                    abs(M._matvec(st.R, M.AXIS_VECTORS[a])[2]) > 0.08 for a in 'XY'):
                continue
        lines = lines_of(st)
        (a, b) = lines['X'][0]
        lines['X'][0] = (a, (b[0] + 0.01, b[1] - 0.005))
        res = M.solve_lines(lines, M.origin_rel(st), 'TWO_VP', 'X', 'Y', st)
        if not res.ok:          # moved into an impossible configuration
            continue
        dist = math.sqrt(sum(c * c for c in st.C))
        assert abs(math.sqrt(sum(c * c for c in res.state.C)) - dist) < 1e-9 * dist
        assert abs(res.state.f - st.f) > 1e-6 or rot_err(res.state.R, st.R) > 1e-6
        # the origin is still seen at the origin point
        o = res.state.project_rel((0, 0, 0))
        assert o is not None and math.dist(o, M.origin_rel(st)) < 1e-6 * max(1.0, abs(o[0]))
        checked += 1
    assert checked > 200, checked


def test_scale_sets_the_known_length_without_changing_the_photo():
    """A known length from the origin along an axis: the camera moves along its
    line to the origin, the photo (rotation, focal, shift, origin) stays."""
    rng = random.Random(31)
    checked = 0
    for i in range(300):
        st = make_state(rng, ASPECTS[i % len(ASPECTS)])
        axis = 'XYZ'[i % 3]
        if not M.scale_axis_usable(st, axis):
            continue
        sign = rng.choice((-1.0, 1.0))
        tip = M.scale_tip(st, axis, sign * rng.uniform(0.3, 3.0))
        if tip is None:
            continue
        length = rng.uniform(0.1, 20.0)
        new, err = M.apply_scale(st, axis, tip, length)
        assert err is None, err
        assert abs(M.scale_measure(new, axis, tip) - sign * length) < 1e-9 * length
        assert rot_err(new.R, st.R) == 0.0 and new.f == st.f and new.pp == st.pp
        assert math.dist(new.project_rel((0, 0, 0)), st.project_rel((0, 0, 0))) < 1e-9
        checked += 1
    assert checked > 150, checked


def test_scale_is_kept_when_lines_move():
    """With a known length the camera's distance comes from it: after moving a
    line the point seen at the end still measures that length."""
    rng = random.Random(33)
    checked = 0
    for i in range(300):
        st = make_state(rng, ASPECTS[i % len(ASPECTS)], pp=(0.0, 0.0))
        axis = 'XYZ'[i % 3]
        tip = M.scale_tip(st, axis, 1.0) if M.scale_axis_usable(st, axis) else None
        if tip is None:
            continue
        lines = lines_of(st)
        (a, b) = lines['X'][0]
        lines['X'][0] = (a, (b[0] + 0.01, b[1] - 0.005))
        res = M.solve_lines(lines, M.origin_rel(st), 'TWO_VP', 'X', 'Y', st,
                            scale=(axis, tip, 2.5))
        if not res.ok or res.warnings:
            continue
        assert abs(abs(M.scale_measure(res.state, axis, tip)) - 2.5) < 1e-9
        o = res.state.project_rel((0, 0, 0))
        assert math.dist(o, M.origin_rel(st)) < 1e-9          # origin stays on its point
        checked += 1
    assert checked > 100, checked


def test_scale_axis_pointing_at_the_camera():
    """An axis along the line of sight to the origin cannot be measured."""
    # camera on the X axis looking at the origin
    st = M.CamState(aspect=1.5, f=1.5, pp=(0.0, 0.0),
                    R=((0.0, 1.0, 0.0), (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)), C=(10.0, 0.0, 0.0))
    assert M.origin_rel(st) is not None
    assert not M.scale_axis_usable(st, 'X')
    assert M.scale_axis_usable(st, 'Z')
    new, err = M.apply_scale(st, 'X', (0.6, 0.5), 2.0)
    assert new is None and 'X axis' in err


def test_scale_end_behind_the_camera_goes_to_the_other_side():
    """The end of a known length is always shown where it measures that length:
    when its point is behind the camera (a length longer than the camera is
    from the origin, the axis pointing towards the camera), on the other side."""
    rng = random.Random(37)
    flipped = kept = 0
    for i in range(600):
        st = make_state(rng, ASPECTS[i % len(ASPECTS)])
        axis = 'XYZ'[i % 3]
        if not M.scale_axis_usable(st, axis):
            continue
        flip = rng.random() < 0.5
        length = rng.uniform(0.2, 2.0) * math.sqrt(sum(c * c for c in st.C))
        tip, new_flip = M.scale_end(st, axis, length, flip)
        assert tip is not None
        s = M.scale_measure(st, axis, tip)
        assert s is not None and abs(abs(s) - length) < 1e-9 * length, (s, length)
        assert (s < 0.0) == new_flip
        own_side = M.scale_tip(st, axis, (-1.0 if flip else 1.0) * length)
        assert (new_flip != flip) == (own_side is None)
        if new_flip != flip:
            flipped += 1
        else:
            kept += 1
    assert flipped > 30 and kept > 100, (flipped, kept)


def test_moving_origin_moves_camera_in_its_own_plane():
    """Dragging the origin point: rotation and focal length stay, the world
    origin keeps its depth, the camera moves parallel to the image plane."""
    rng = random.Random(11)
    for i in range(200):
        st = make_state(rng, ASPECTS[i % len(ASPECTS)], pp=(0.0, 0.0))
        o = M.origin_rel(st)
        new_o = (o[0] + 0.03, o[1] + 0.02)
        res = M.solve_lines(lines_of(st), new_o, 'TWO_VP', 'X', 'Y', st, origin_moved=True)
        assert res.ok and not res.origin_follows
        new = res.state
        assert rot_err(new.R, st.R) < 1e-9 and abs(new.f - st.f) < 1e-9
        assert abs(new.to_cam((0, 0, 0))[2] - st.to_cam((0, 0, 0))[2]) < 1e-9     # same depth
        move = M._matvec(st.R, tuple(a - b for a, b in zip(new.C, st.C)))
        assert abs(move[2]) < 1e-9                                                # in its plane
        assert math.dist(new.project_rel((0, 0, 0)), new_o) < 1e-9


def test_shift_by_mode():
    """Switching the mode (reset_shift) starts without shift: 2 Points / 1 Point
    end with shift 0, Parallel Verticals with the principal point on the horizon
    (no tilt). Editing lines afterwards keeps the shift the camera has."""
    rng = random.Random(31)
    for i in range(100):
        st = make_state(rng, 1.5, pp=(0.15, -0.1))          # a camera with a big shift
        for mode in ('TWO_VP', 'ONE_VP', 'VERTICALS'):
            res = M.solve_lines(lines_of(st), M.origin_rel(st), mode, 'X', 'Y', st,
                                reset_shift=True)
            if not res.ok:
                continue
            if mode == 'VERTICALS':
                d = M._matvec(res.state.R, (0, 0, 1))                 # up in camera space
                assert abs(d[2]) < 1e-12 and abs(d[0]) < 1e-12      # no tilt, no roll
                assert res.state.pp[0] == 0.0                        # Shift X 0 after the switch
            else:
                assert res.state.pp == (0.0, 0.0)
        for mode in ('TWO_VP', 'ONE_VP'):                           # moving lines: shift kept
            res = M.solve_lines(lines_of(st), M.origin_rel(st), mode, 'X', 'Y', st)
            assert res.ok and math.dist(res.state.pp, st.pp) < 1e-12


def euler_xyz_deg(R):
    """Blender XYZ Euler (degrees) of a camera with world->camera rotation R."""
    m = M._transpose(R)                       # camera -> world, columns = camera axes
    y = math.asin(max(-1.0, min(1.0, -m[2][0])))
    x = math.atan2(m[2][1], m[2][2])
    z = math.atan2(m[1][0], m[0][0])
    return tuple(math.degrees(a) for a in (x, y, z))


def test_parallel_verticals_is_exactly_level():
    """Lines of a tilted AND rolled camera: Parallel Verticals gives exactly
    Rotation X 90 / Y 0, a horizontal horizon fitted to both pairs (least
    squares), the principal point on it at the camera's Shift X, upright."""
    rng = random.Random(41)
    for i in range(300):
        st = make_state(rng, ASPECTS[i % len(ASPECTS)])            # tilt and roll
        lines = lines_of(st)
        if i % 2:                                                   # a bit of noise
            lines = {a: [((p[0] + rng.uniform(-2e-3, 2e-3), p[1]), q) for p, q in segs]
                     for a, segs in lines.items()}
        a1, a2 = ('X', 'Y') if i % 3 else ('Y', 'X')
        res = M.solve_lines(lines, M.origin_rel(st), 'VERTICALS', a1, a2, st)
        if not res.ok:          # impossible configurations are allowed to fail
            continue
        new = res.state
        rx, ry, _rz = euler_xyz_deg(new.R)
        assert abs(rx - 90.0) < 1e-9 and abs(ry) < 1e-9, (rx, ry)
        assert abs(new.pp[0] - st.pp[0]) < 1e-12                    # Shift X kept
        h = new.horizon_rel()
        assert abs(h[0][1] - h[1][1]) < 1e-12                       # horizontal horizon
        assert abs(S.plane_to_rel(new.pp, new.aspect)[1] - h[0][1]) < 1e-12
        # both vanishing points sit on that horizon
        for a in (a1, a2):
            vp = new.vp_rel(a)
            assert vp is None or abs(vp[1] - h[0][1]) < 1e-9


def test_parallel_verticals_always_uses_x_and_y():
    """The axis fields do not matter in Parallel Verticals: X and Y are used."""
    st = make_state(random.Random(43), 1.5)
    ref = M.solve_lines(lines_of(st), M.origin_rel(st), 'VERTICALS', 'X', 'Y', st)
    for a1, a2 in (('X', 'Z'), ('Z', 'Y'), ('Y', 'X'), ('Z', 'Z')):
        res = M.solve_lines(lines_of(st), M.origin_rel(st), 'VERTICALS', a1, a2, st)
        assert res.ok and rot_err(res.state.R, ref.state.R) < 1e-12
    assert M.used_axes('VERTICALS', 'X', 'Z') == ['X', 'Y']


def test_errors_name_the_axes():
    st = make_state(random.Random(47), 1.5)
    lines = lines_of(st)
    a, b = lines['Y'][0]
    lines['Y'][1] = ((a[0], a[1] + 0.1), (b[0], b[1] + 0.1))        # parallel Y lines
    res = M.solve_lines(lines, M.origin_rel(st), 'TWO_VP', 'X', 'Y', st)
    assert not res.ok and res.errors[0].startswith('Y lines'), res.errors
    assert not any('VP' in e for e in res.errors)


def test_position_for_origin_brings_origin_back():
    """Reset Camera Position: rotation, focal length and shift stay; the result
    is the same wherever the camera was (pressing it again changes nothing),
    the origin is in the frame below the horizon, on the ground at the kept
    height, at a sensible distance (also for level cameras with a low horizon)."""
    rng = random.Random(53)
    for i in range(300):
        level = i % 2 == 0
        st = make_state(rng, ASPECTS[i % len(ASPECTS)], level=level,
                        pp=(0.0, rng.uniform(-0.25, 0.25)) if level else None)
        spots = []
        for _k in range(3):                                  # wherever the camera ran off to
            gone = M.CamState(st.aspect, st.f, st.pp, st.R,
                              (rng.uniform(-3000, 3000), rng.uniform(-3000, 3000), st.C[2]))
            spots.append(M.position_for_origin(gone, 1.6, 10.0))
        C = spots[0]
        assert all(math.dist(C, c) < 1e-6 for c in spots)
        new = M.CamState(st.aspect, st.f, st.pp, st.R, C)
        again = M.position_for_origin(new, new.C[2], 10.0)
        assert math.dist(again, C) < 1e-6                    # idempotent
        seen = new.project_rel((0.0, 0.0, 0.0))
        target, _hy = M.reset_target(new)
        assert seen is not None and math.dist(seen, target) < 1e-9
        assert 0.0 <= seen[0] <= 1.0 and 0.0 <= seen[1] <= 1.0
        assert math.dist(C, (0.0, 0.0, 0.0)) < 100.0 * 1.6 + 10.0


def euler_to_world_matrix(loc, euler_deg):
    """4x4 rows of a Blender matrix_world from location and XYZ Euler (degrees)."""
    x, y, z = (math.radians(a) for a in euler_deg)
    cx, sx, cy, sy, cz, sz = math.cos(x), math.sin(x), math.cos(y), math.sin(y), math.cos(z), math.sin(z)
    r = ((cy * cz, sx * sy * cz - cx * sz, cx * sy * cz + sx * sz),
         (cy * sz, sx * sy * sz + cx * cz, cx * sy * sz - sx * cz),
         (-sy, sx * cy, cx * cy))
    return [list(r[0]) + [loc[0]], list(r[1]) + [loc[1]], list(r[2]) + [loc[2]], [0, 0, 0, 1]]


def upright(st):
    return M._matvec(st.R, (0.0, 0.0, 1.0))[1] > 0.0


def test_bug_z_down_file():
    """Regression (numbers from a saved scene): an upside-down camera (its up
    pointing down) with 2 Points X / Y lines. Solving must turn it upright again."""
    lines = {'X': [((0.8357982635498047, 0.9097976088523865), (0.8701854348182678, 0.8832356929779053)),
                   ((0.8994303941726685, 0.7315829396247864), (0.9222701191902161, 0.7267746925354004))],
             'Y': [((0.5804248452186584, 0.8341259360313416), (0.8357982635498047, 0.9097976088523865)),
                   ((0.5789465308189392, 0.8890076279640198), (0.8362645506858826, 0.991225004196167))]}
    mw = euler_to_world_matrix((-6.279865741729736, 8.194719314575195, -0.6533538699150085),
                               (-79.15592289511319, 0.3551701036449275, 40.84765263312695))
    cur = M.state_from_blender(mw, 19.20648956298828, 0.0, 0.0, 'AUTO', 36.0, 24.0, 1624 / 941)
    assert not upright(cur)
    res = M.solve_lines(lines, (0.46727532148361206, 0.7372727990150452), 'TWO_VP', 'X', 'Y', cur)
    assert res.ok and upright(res.state)


def test_never_upside_down():
    """Whatever the current camera (even upside down) and however the
    vanishing points moved (X and Y changing sides), the solved camera is
    upright and matches the lines."""
    rng = random.Random(59)
    flip = ((-1.0, 0.0, 0.0), (0.0, -1.0, 0.0), (0.0, 0.0, 1.0))      # roll by 180 degrees
    for i in range(300):
        aspect = ASPECTS[i % len(ASPECTS)]
        mode = M.MODES[i % 4]
        level = mode == 'VERTICALS'
        st = make_state(rng, aspect, level=level, pp=pp_for(mode, rng))
        a1, a2 = ('X', 'Y') if level or i % 3 == 0 else PAIRS[i % len(PAIRS)]
        lines = lines_of(st)
        # current camera: upside down, or turned 90 degrees about Z (the X and Y
        # vanishing points change sides compared with it)
        if i % 2:
            cur = M.CamState(aspect, st.f, st.pp, M._matmul(flip, st.R), st.C)
        else:
            c, s_ = math.cos(math.pi / 2), math.sin(math.pi / 2)
            rz = ((c, -s_, 0.0), (s_, c, 0.0), (0.0, 0.0, 1.0))
            cur = M.CamState(aspect, st.f, st.pp, M._matmul(st.R, M._transpose(rz)), st.C)
        res = M.solve_lines(lines, M.origin_rel(st), mode, a1, a2, cur)
        if not res.ok:
            continue
        assert upright(res.state), (mode, a1, a2, i)
        for axis in M.used_axes(mode, a1, a2):         # still on the lines
            want, got = st.vp_rel(axis), res.state.vp_rel(axis)
            if want is not None and got is not None and max(map(abs, want)) < 50:
                assert math.dist(want, got) < 1e-6, (mode, axis, want, got)


def test_principal_point_moved_by_mode():
    """The principal point is shown as moved (another colour) when it is away
    from the mode's default: the centre (2 / 1 Point), the centre column
    (Parallel Verticals: it always sits on the horizon); 3 Points computes it."""
    for mode in ('TWO_VP', 'ONE_VP'):
        assert not M.principal_point_moved(mode, (0.0, 0.0))
        assert M.principal_point_moved(mode, (0.0, 0.01))
        assert M.principal_point_moved(mode, (0.01, 0.0))
    assert not M.principal_point_moved('VERTICALS', (0.0, 0.2))     # on the horizon
    assert M.principal_point_moved('VERTICALS', (0.01, 0.2))
    assert not M.principal_point_moved('THIRD_VP', (0.3, 0.2))


def test_axis_sign_follows_current_camera():
    """X axis almost parallel to the image: a small turn moves its vanishing
    point through infinity to the other side. Solving from the turned lines,
    starting from the previous camera, must give the turned camera, not a
    camera flipped by 180 degrees."""
    rng = random.Random(13)
    for i in range(100):
        h = rng.uniform(0.5, 4.0)
        loc = (rng.uniform(-0.5, 0.5), -rng.uniform(5, 12), h)
        fwd = norm((0.0, 1.0, -rng.uniform(0.05, 0.4)))          # looks along +Y, X sideways
        right = norm(cross(fwd, (0, 0, 1)))
        up = cross(right, fwd)
        st = M.CamState(1.5, rng.uniform(1.0, 2.5), (0.0, 0.0), (right, up, tuple(-c for c in fwd)), loc)
        for ang in (0.01, -0.01, 0.03, -0.03):
            c, s_ = math.cos(ang), math.sin(ang)
            rz = ((c, -s_, 0.0), (s_, c, 0.0), (0.0, 0.0, 1.0))
            st1 = M.CamState(st.aspect, st.f, st.pp, M._matmul(st.R, M._transpose(rz)), st.C)
            lines1 = M.retarget(st1, lines_of(st))
            res = M.solve_lines(lines1, M.origin_rel(st1), 'TWO_VP', 'X', 'Z', st)
            assert res.ok, res.errors
            assert rot_err(res.state.R, st1.R) < 1e-7, (ang, rot_err(res.state.R, st1.R))


def test_one_vp_minimal_rotation():
    rng = random.Random(17)
    for i in range(200):
        st = make_state(rng, ASPECTS[i % len(ASPECTS)], pp=(0.0, 0.0))
        lines = lines_of(st)
        (a, b) = lines['Z'][1]
        lines['Z'][1] = ((a[0] + 0.01, a[1]), b)
        res = M.solve_lines(lines, M.origin_rel(st), 'ONE_VP', 'Z', 'X', st)
        assert res.ok, res.errors
        new = res.state
        assert new.f == st.f and new.pp == st.pp
        # the Z direction now points at the vanishing point of the Z lines
        segs = [(S.rel_to_plane(p, st.aspect), S.rel_to_plane(q, st.aspect)) for p, q in lines['Z']]
        vp, _ = S.vanishing_point(segs)
        d = M._matvec(new.R, (0, 0, 1))
        img = (new.pp[0] + new.f * d[0] / -d[2], new.pp[1] + new.f * d[1] / -d[2])
        assert math.dist(img, vp) < 1e-7 * max(1.0, math.hypot(*vp))
        # smallest rotation: the rotation angle equals the angle the Z axis moved
        z0, z1 = M._matvec(st.R, (0, 0, 1)), d
        moved = math.acos(max(-1.0, min(1.0, M._dot(z0, z1))))
        q = M._matmul(new.R, M._transpose(st.R))
        total = math.acos(max(-1.0, min(1.0, (q[0][0] + q[1][1] + q[2][2] - 1) / 2)))
        assert abs(total - moved) < 1e-7


def test_lines_built_without_visible_origin():
    rng = random.Random(19)
    st = make_state(rng, 1.5, pp=(0.0, 0.0))
    # turn the camera around: origin behind it
    flip = ((-1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, -1.0))
    st2 = M.CamState(st.aspect, st.f, st.pp, M._matmul(flip, st.R), st.C)
    assert M.origin_rel(st2) is None
    lines = lines_of(st2)
    res = M.solve_lines(lines, None, 'TWO_VP', 'X', 'Y', st2)
    assert res.ok and state_err(res.state, st2) < 1e-8


def test_signatures():
    assert M.signatures_equal([1.0, 2.0], [1.0, 2.0 + 1e-9])
    assert not M.signatures_equal([1.0, 2.0], [1.0, 2.001])
    assert not M.signatures_equal(None, [1.0])


if __name__ == '__main__':
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'PASS {name}')
            except AssertionError as e:
                failed += 1
                print(f'FAIL {name}: {e!r}')
    sys.exit(1 if failed else 0)
