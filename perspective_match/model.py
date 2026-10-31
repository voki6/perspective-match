# SPDX-License-Identifier: GPL-3.0-or-later
"""The camera is the single source of truth; the lines on the photo are an
interactive picture of it. This module holds the math for both directions,
without bpy (tested by tests/test_model.py):

camera -> lines   vanishing points of the world axes, the horizon and the
                  projected world origin are computed from the camera; stored
                  lines are "re-aimed" at the new vanishing points (rotated about
                  their middle, length kept) or built from the camera.
lines -> camera   the solver turns lines into a rotation and focal length; the
                  principal point (lens shift) follows the mode (see _PP_MODE), the
                  axis signs keep the camera upright and closest to the current
                  camera (orient_axes), and the camera keeps its distance to
                  the world origin (dragging the origin point moves the camera
                  in its own plane).
scale             optional: a known length from the world origin along one axis
                  (its end is a point on the photo) sets how far the camera is
                  from the origin; the photo does not change (apply_scale).

Frames: see solver.py (relative 0..1 y-down, image plane centred y-up with the
long side spanning [-1, 1], camera x right / y up / looking along -z).
"""

import math
from dataclasses import dataclass

try:
    from . import solver as S
except ImportError:          # imported as a top level module by the tests
    import solver as S

AXIS_VECTORS = {'X': (1.0, 0.0, 0.0), 'Y': (0.0, 1.0, 0.0), 'Z': (0.0, 0.0, 1.0)}
UP = (0.0, 0.0, 1.0)

# Modes of the "lines -> camera" direction
MODES = ('TWO_VP', 'VERTICALS', 'THIRD_VP', 'ONE_VP')
# Principal point (lens shift) per mode. It starts from the camera's current
# shift; switching the mode starts it from the image centre instead
# (reset_shift=True), so a fresh mode begins without shift:
#   2 Points, 1 Point   kept as it is (0 after switching to the mode)
#   Parallel Verticals  a LEVEL camera (Rotation X 90, Y 0): the horizon is a
#                       horizontal image line fitted to both pairs of lines,
#                       the principal point sits on it (Shift Y does the tilt),
#                       Shift X is kept
#   3 Points            computed from the three vanishing points
_PP_MODE = {'TWO_VP': 'MANUAL', 'VERTICALS': 'LEVEL', 'THIRD_VP': 'THIRD_VP'}
CENTRE = (0.0, 0.0)


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a):
    n = math.sqrt(_dot(a, a))
    return (a[0] / n, a[1] / n, a[2] / n) if n > 0.0 else (0.0, 0.0, 0.0)


def _matvec(m, v):
    return (_dot(m[0], v), _dot(m[1], v), _dot(m[2], v))


def _transpose(m):
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


def _matmul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
                 for i in range(3))


def third_axis(a1, a2):
    rest = [a for a in 'XYZ' if a not in (a1, a2)]
    return rest[0] if len(rest) == 1 else None


def principal_point_moved(mode, pp, tol=1e-5):
    """Is the principal point (lens shift) away from where the mode puts it by
    default? 2 Points / 1 Point: the image centre; Parallel Verticals: the
    centre column (it always sits on the horizon); 3 Points computes it."""
    if mode in ('TWO_VP', 'ONE_VP'):
        return math.hypot(pp[0], pp[1]) > tol
    if mode == 'VERTICALS':
        return abs(pp[0]) > tol
    return False


def mode_axes(mode, a1, a2):
    """The two axes a mode really uses: Parallel Verticals always X and Y (the
    two horizontal directions; verticals have no vanishing point there),
    3 Points all three (any two different ones as the first pair)."""
    if mode == 'VERTICALS' or (mode == 'THIRD_VP' and a1 == a2):
        return 'X', 'Y'
    return a1, a2


def used_axes(mode, a1, a2):
    """Axes whose lines are inputs of the solve in this mode."""
    a1, a2 = mode_axes(mode, a1, a2)
    if mode == 'ONE_VP':
        return [a1]
    if mode == 'THIRD_VP':
        t = third_axis(a1, a2)
        return [a1, a2] + ([t] if t else [])
    return [a1, a2]


# --------------------------------------------------------------------------
# Camera state
# --------------------------------------------------------------------------

@dataclass
class CamState:
    aspect: float                  # photo width / height
    f: float                       # relative focal length (see solver.py)
    pp: tuple                      # principal point, image plane
    R: tuple                       # 3x3 rows: world -> camera rotation
    C: tuple                       # camera location (world)

    def to_cam(self, p):
        return _matvec(self.R, (p[0] - self.C[0], p[1] - self.C[1], p[2] - self.C[2]))

    def project(self, p):
        """World point -> image plane, None if not in front of the camera."""
        c = self.to_cam(p)
        if c[2] >= -1e-9:
            return None
        return (self.pp[0] + self.f * c[0] / -c[2], self.pp[1] + self.f * c[1] / -c[2])

    def project_rel(self, p):
        q = self.project(p)
        return None if q is None else S.plane_to_rel(q, self.aspect)

    def vp_h(self, direction):
        """Vanishing point of a world direction in homogeneous image plane
        coordinates (x, y, w); w == 0 means at infinity."""
        d = _matvec(self.R, direction)
        w = -d[2]
        return (self.pp[0] * w + self.f * d[0], self.pp[1] * w + self.f * d[1], w)

    def vp_rel(self, axis):
        x, y, w = self.vp_h(AXIS_VECTORS[axis])
        if abs(w) * 1e7 <= math.hypot(x, y):
            return None
        return S.plane_to_rel((x / w, y / w), self.aspect)

    def as_solution(self):
        """A solver.Solution with this camera (for horizon / Blender mapping)."""
        t = _matvec(self.R, tuple(-c for c in self.C))
        return S.Solution(ok=True, aspect=self.aspect, focal_rel=self.f,
                          principal_point=self.pp, view_rotation=self.R,
                          camera_rotation=_transpose(self.R), camera_location=self.C,
                          view_translation=t)

    def horizon_rel(self):
        return self.as_solution().horizon_rel(UP)


def state_from_blender(matrix_world, lens, shift_x, shift_y, sensor_fit, sensor_width,
                       sensor_height, aspect):
    """matrix_world: 4x4 rows (scale is ignored)."""
    _fit, s = S._fit_half_extent(aspect, sensor_fit)
    size = S._sensor_size(sensor_fit, sensor_width, sensor_height)
    f = lens * 2.0 * s / size
    pp = (-shift_x * 2.0 * s, -shift_y * 2.0 * s)
    cols = [_norm((matrix_world[0][j], matrix_world[1][j], matrix_world[2][j])) for j in range(3)]
    R = tuple(tuple(c) for c in cols)            # rows of world->camera = camera axes
    C = (matrix_world[0][3], matrix_world[1][3], matrix_world[2][3])
    return CamState(aspect=aspect, f=f, pp=pp, R=R, C=C)


def blender_settings(state, sensor_fit, sensor_width, sensor_height):
    """lens, shift_x, shift_y, rotation (3x3 camera->world rows) and location."""
    _fit, s = S._fit_half_extent(state.aspect, sensor_fit)
    size = S._sensor_size(sensor_fit, sensor_width, sensor_height)
    return {
        'lens': state.f / s * size / 2.0,
        'shift_x': -state.pp[0] / (2.0 * s),
        'shift_y': -state.pp[1] / (2.0 * s),
        'rotation': _transpose(state.R),
        'location': state.C,
    }


# --------------------------------------------------------------------------
# camera -> lines
# --------------------------------------------------------------------------

def retarget_segment(seg_rel, vp_h, aspect):
    """Rotate a segment about its middle so that its line passes through the
    (homogeneous, image plane) vanishing point; the length (in image plane
    units) and the order of its ends are kept."""
    a = S.rel_to_plane(seg_rel[0], aspect)
    b = S.rel_to_plane(seg_rel[1], aspect)
    m = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
    half = math.hypot(b[0] - a[0], b[1] - a[1]) / 2.0
    x, y, w = vp_h
    dx, dy = x - m[0] * w, y - m[1] * w          # direction m -> vp (up to sign)
    n = math.hypot(dx, dy)
    if n < 1e-12 or half <= 0.0:
        return seg_rel
    ux, uy = dx / n, dy / n
    if ux * (b[0] - a[0]) + uy * (b[1] - a[1]) < 0.0:
        ux, uy = -ux, -uy
    na = (m[0] - ux * half, m[1] - uy * half)
    nb = (m[0] + ux * half, m[1] + uy * half)
    return (S.plane_to_rel(na, aspect), S.plane_to_rel(nb, aspect))


def retarget(state, lines):
    """lines: {'X': [(a, b), ...], ...} (relative) -> re-aimed copy."""
    return {axis: [retarget_segment(seg, state.vp_h(AXIS_VECTORS[axis]), state.aspect)
                   for seg in segs]
            for axis, segs in lines.items()}


# Edges of a box [0, L]^3 at the world origin, middle 60 % only, so that lines of
# different axes never share an end point (or the origin point).
def _box_edges(axis, size):
    t0, t1 = 0.2 * size, 0.8 * size
    L = size
    return {
        'X': [((t0, L, 0.0), (t1, L, 0.0)), ((t0, L, L), (t1, L, L))],
        'Y': [((L, t0, 0.0), (L, t1, 0.0)), ((L, t0, L), (L, t1, L))],
        'Z': [((L, 0.0, t0), (L, 0.0, t1)), ((0.0, L, t0), (0.0, L, t1))],
    }[axis]


# Image-space fallback: middles of the two lines of each axis (relative)
_FALLBACK_MIDS = {'X': ((0.68, 0.38), (0.68, 0.68)),
                  'Y': ((0.32, 0.38), (0.32, 0.68)),
                  'Z': ((0.42, 0.55), (0.58, 0.55))}


def build_segments(state, axis):
    """Two line segments for an axis, built from the camera: the projected
    edges of a box at the world origin when it is in view, otherwise lines
    through fixed points of the image aimed at the vanishing point."""
    o = state.to_cam((0.0, 0.0, 0.0))
    if o[2] < -1e-6:
        size = 0.6 * (-o[2]) / max(state.f, 1e-6)
        hx, hy = S.plane_half_extent(state.aspect)
        segs = []
        for p0, p1 in _box_edges(axis, size):
            q0, q1 = state.project(p0), state.project(p1)
            if q0 is None or q1 is None:
                break
            if max(abs(q0[0]), abs(q1[0])) > 2.5 * hx or max(abs(q0[1]), abs(q1[1])) > 2.5 * hy:
                break
            if math.hypot(q1[0] - q0[0], q1[1] - q0[1]) < 0.04:
                break
            segs.append((S.plane_to_rel(q0, state.aspect), S.plane_to_rel(q1, state.aspect)))
        if len(segs) == 2:
            return segs
    vp = state.vp_h(AXIS_VECTORS[axis])
    out = []
    for mid in _FALLBACK_MIDS[axis]:
        m = S.rel_to_plane(mid, state.aspect)
        dummy = (S.plane_to_rel((m[0] - 0.2, m[1]), state.aspect),
                 S.plane_to_rel((m[0] + 0.2, m[1]), state.aspect))
        out.append(retarget_segment(dummy, vp, state.aspect))
    return out


def origin_rel(state):
    """Projected world origin (relative) or None when it is not in front."""
    return state.project_rel((0.0, 0.0, 0.0))


# --------------------------------------------------------------------------
# lines -> camera
# --------------------------------------------------------------------------

def _rot_dist(a, b):
    return sum((a[i][j] - b[i][j]) ** 2 for i in range(3) for j in range(3))


def _rotation_between(u, v):
    """Rotation matrix taking unit vector u onto unit vector v (shortest arc)."""
    k = _cross(u, v)
    s = math.sqrt(_dot(k, k))
    c = _dot(u, v)
    if s < 1e-15:
        if c > 0.0:
            return ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        # 180 degrees: any axis perpendicular to u
        p = _norm(_cross(u, (1.0, 0.0, 0.0) if abs(u[0]) < 0.9 else (0.0, 1.0, 0.0)))
        return tuple(tuple(2.0 * p[i] * p[j] - (1.0 if i == j else 0.0) for j in range(3))
                     for i in range(3))
    k = (k[0] / s, k[1] / s, k[2] / s)
    t = 1.0 - c
    x, y, z = k
    return ((c + x * x * t, x * y * t - z * s, x * z * t + y * s),
            (y * x * t + z * s, c + y * y * t, y * z * t - x * s),
            (z * x * t - y * s, z * y * t + x * s, c + z * z * t))


def _origin_ray(R, f, pp, aspect, origin):
    """World direction from the camera to where `origin` (relative) is seen,
    scaled so that its depth in front of the camera is 1."""
    o = S.rel_to_plane(origin, aspect)
    return _matvec(_transpose(R), ((o[0] - pp[0]) / f, (o[1] - pp[1]) / f, -1.0))


def place_camera_in_view(R, f, pp, aspect, origin, current):
    """Moving the origin point moves the camera in ITS OWN plane (parallel to the
    image): the world origin keeps its depth in front of the camera."""
    if origin is None:
        return tuple(current.C), False
    depth = -current.to_cam((0.0, 0.0, 0.0))[2]
    if depth <= 1e-9:
        return tuple(current.C), False
    ray = _origin_ray(R, f, pp, aspect, origin)  # world origin = C + depth * ray
    return (-depth * ray[0], -depth * ray[1], -depth * ray[2]), True


def place_camera(R, f, pp, aspect, origin, current_location):
    """(location, origin_honoured). The camera keeps its distance to the world
    origin and is moved so that the origin is seen at `origin` (relative), as
    fSpy does: however the lines move, the camera never runs off. Without an
    origin point it stays where it is and the origin point follows the camera."""
    d = math.sqrt(_dot(current_location, current_location))
    if origin is None or d <= 1e-12:
        return tuple(current_location), False
    ray = _norm(_origin_ray(R, f, pp, aspect, origin))
    return (-d * ray[0], -d * ray[1], -d * ray[2]), True


def _upright(R):
    """World up (Z) does not point down in the camera: the camera is not upside
    down (roll within +-90 degrees). R: world -> camera."""
    return R[1][2] >= -1e-9


def _half_turn(u):
    """Rotation by 180 degrees about the unit vector u."""
    return tuple(tuple(2.0 * u[i] * u[j] - (1.0 if i == j else 0.0) for j in range(3))
                 for i in range(3))


def orient_axes(R, a1, a2, current_R):
    """R was solved for +a1 / +a2. Line directions give each axis only up to its
    sign, so there are four candidates: world axis a goes to +-(R e_a), the
    third axis follows (s1 * s2) so each candidate is a rotation. Rules, in
    this order:
      1. the camera is never upside down (world Z points up in the camera),
      2. the candidate closest to the current camera (no flips while editing),
      3. on a tie, the one looking the same way as the current camera.
    Rule 1 is what keeps two vanishing points that change sides from turning
    the camera over (each axis alone would still agree with the current one)."""
    i1, i2 = 'XYZ'.index(a1), 'XYZ'.index(a2)
    best = None
    for s1 in (1.0, -1.0):
        for s2 in (1.0, -1.0):
            d = [s1 * s2] * 3
            d[i1], d[i2] = s1, s2
            cand = tuple(tuple(R[i][j] * d[j] for j in range(3)) for i in range(3))
            key = (0 if _upright(cand) else 1, round(_rot_dist(cand, current_R), 6),
                   -_dot(cand[2], current_R[2]))
            if best is None or key < best[0]:
                best = (key, cand)
    return best[1]


def reset_target(state):
    """Where Reset Camera Position puts the world origin on the photo: the middle
    column, halfway between the horizon and the bottom edge (clamped into the
    frame), so it is on the ground at a sensible distance. Also returns the
    horizon height there (relative y, larger = lower; None without horizon)."""
    h = state.horizon_rel()
    if h is None:
        return (0.5, 0.5), None
    (x0, y0), (x1, y1) = h
    hy = 0.5 if abs(x1 - x0) < 1e-12 else y0 + (y1 - y0) * (0.5 - x0) / (x1 - x0)
    return (0.5, min(max(hy + 0.5 * (1.0 - hy), 0.5), 0.95)), hy


def position_for_origin(state, height, depth):
    """Reset Camera Position: the camera location that shows the world origin at
    reset_target(); rotation, focal length and shift stay, so the result does
    not depend on where the camera was (pressing it again changes nothing).
    The origin is on the ground with the camera `height` above it (the new
    camera height, 1.6 m); when there
    is (almost) no ground below the horizon in the frame, it is put `depth` in
    front of the camera instead."""
    target, hy = reset_target(state)
    ray = _origin_ray(state.R, state.f, state.pp, state.aspect, target)
    down = -_dot(ray, UP)
    k = depth                                     # world origin = C + k * ray
    if (hy is None or target[1] - hy >= 0.05) and down > 1e-9:
        k = height / down                         # on the ground, camera `height` above it
    return (-k * ray[0], -k * ray[1], -k * ray[2])


# --------------------------------------------------------------------------
# Scale: a known length from the world origin along one axis
# --------------------------------------------------------------------------

# An axis pointing closer than this to the line of sight to the origin cannot
# be measured: on the photo it shrinks to (almost) a point.
SCALE_MIN_ANGLE = math.radians(10.0)


def scale_axis_usable(state, axis):
    """The axis can be measured: the world origin is in front of the camera and
    the axis does not point (almost) along the line of sight to it."""
    t = state.to_cam((0.0, 0.0, 0.0))
    if t[2] >= -1e-9:
        return False
    a = _matvec(state.R, AXIS_VECTORS[axis])
    return abs(_dot(_norm(t), a)) < math.cos(SCALE_MIN_ANGLE)


def scale_tip(state, axis, length):
    """Where the point `length` (signed) along `axis` from the world origin is
    seen on the photo (relative), or None."""
    return state.project_rel(tuple(length * c for c in AXIS_VECTORS[axis]))


def scale_measure(state, axis, tip):
    """Signed length from the world origin along `axis` to the point seen at
    `tip` (relative): the point of the axis closest to that line of sight.
    None when it cannot be measured."""
    if not scale_axis_usable(state, axis):
        return None
    t = state.to_cam((0.0, 0.0, 0.0))                  # the origin, camera space
    a = _matvec(state.R, AXIS_VECTORS[axis])
    p = S.rel_to_plane(tip, state.aspect)
    r = ((p[0] - state.pp[0]) / state.f, (p[1] - state.pp[1]) / state.f, -1.0)
    # closest points of the lines k * r (line of sight) and t + s * a (axis)
    rr, ra = _dot(r, r), _dot(r, a)
    den = rr - ra * ra                                 # a is a unit vector
    if den <= 1e-12 * rr:
        return None
    rt, at = _dot(r, t), _dot(a, t)
    k = (rt - ra * at) / den
    if k <= 0.0:                                       # behind the camera
        return None
    return (ra * rt - rr * at) / den


def apply_scale(state, axis, tip, length):
    """(state, error): the camera moved along its line to the world origin so
    that the point seen at `tip` is `length` from the origin along `axis`. Only
    the size of the world changes, the photo stays the same."""
    s = scale_measure(state, axis, tip)
    if s is None:
        return None, f'scale: the {axis} axis cannot be measured from here (pick another axis)'
    if abs(s) < 1e-9 * max(1.0, math.sqrt(_dot(state.C, state.C))):
        return None, 'scale: the end of the measured length is at the origin'
    k = length / abs(s)
    return CamState(aspect=state.aspect, f=state.f, pp=state.pp, R=state.R,
                    C=tuple(k * c for c in state.C)), None


@dataclass
class SolveResult:
    state: CamState = None
    errors: list = None
    warnings: list = None
    origin_follows: bool = False    # the origin point must be moved onto the camera's origin

    @property
    def ok(self):
        return self.state is not None


def solve_lines(lines, origin, mode, a1, a2, current, origin_moved=False, reset_shift=False,
                pp=None, scale=None):
    """New camera from the lines. lines: {'X': [(a, b), ...], ...} (relative),
    origin: relative point of the world origin or None, current: CamState.
    origin_moved: the origin point itself is being dragged (the camera then
    moves in its own plane instead of keeping its distance to the origin).
    reset_shift: start the principal point from the image centre (mode switch).
    pp: start it from this point instead (image plane; the principal point
    handle is being dragged).
    scale: (axis, tip, length) - the known length sets the camera's distance to
    the origin (apply_scale) instead of keeping it; not while the origin point
    itself is dragged (that keeps the size of the world)."""
    seed = pp if pp is not None else (CENTRE if reset_shift else current.pp)
    aspect = current.aspect
    warnings = []
    if mode not in MODES:
        return SolveResult(None, [f'unknown mode {mode!r}'], [])
    a1, a2 = mode_axes(mode, a1, a2)
    if mode != 'ONE_VP' and a1 == a2:
        return SolveResult(None, ['the two vanishing points must use different axes'], [])

    if mode == 'ONE_VP':
        segs = [(S.rel_to_plane(a, aspect), S.rel_to_plane(b, aspect)) for a, b in lines.get(a1, [])]
        vp, err = S.vanishing_point(segs)
        if vp is None:
            return SolveResult(None, [f'{a1} lines: {err}'], [])
        f, pp = current.f, seed
        u = _norm((vp[0] - pp[0], vp[1] - pp[1], -f))
        u_cur = _matvec(current.R, AXIS_VECTORS[a1])
        if a1 == 'Z' and abs(u[1]) > 1e-9:
            if u[1] < 0.0:                       # world up points up in the camera
                u = (-u[0], -u[1], -u[2])
        elif _dot(u, u_cur) < 0.0:
            u = (-u[0], -u[1], -u[2])
        R = _matmul(_rotation_between(u_cur, u), current.R)
        if a1 != 'Z' and not _upright(R):
            R = _matmul(_half_turn(u), R)        # same vanishing point, not upside down
    else:
        t = third_axis(a1, a2)
        pts = S.ControlPoints(vp1=lines.get(a1, []), vp2=lines.get(a2, []),
                              vp3=lines.get(t, []) if t else [],
                              principal_point=S.plane_to_rel(seed, aspect))
        settings = S.Settings(mode='2VP', pp_mode=_PP_MODE[mode], vp1_axis='+' + a1,
                              vp2_axis='+' + a2,
                              labels=(f'{a1} lines', f'{a2} lines', f'{t} lines' if t else ''))
        sol = S.solve(pts, settings, aspect)
        if not sol.ok:
            return SolveResult(None, sol.errors or ['cannot solve'], [])
        warnings = list(sol.warnings)
        R = orient_axes(sol.view_rotation, a1, a2, current.R)
        f, pp = sol.focal_rel, sol.principal_point

    if origin_moved:
        C, kept = place_camera_in_view(R, f, pp, aspect, origin, current)
    else:
        C, kept = place_camera(R, f, pp, aspect, origin, current.C)
    st = CamState(aspect=aspect, f=f, pp=pp, R=R, C=C)
    if scale is not None and kept and not origin_moved:
        scaled, err = apply_scale(st, *scale)
        if scaled is None:
            warnings.append(err)
        else:
            st = scaled
    return SolveResult(st, [], warnings, origin_follows=not kept)


# --------------------------------------------------------------------------
# Change detection
# --------------------------------------------------------------------------

def signatures_equal(a, b, tol=1e-6):
    if a is None or b is None or len(a) != len(b):
        return False
    return all(abs(x - y) <= tol * max(1.0, abs(x), abs(y)) for x, y in zip(a, b))
