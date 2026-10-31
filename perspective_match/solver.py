# SPDX-License-Identifier: GPL-3.0-or-later
#
# Perspective Match - camera calibration from vanishing points for Blender.
#
# The solver is a Python port of the fSpy solver
#   fSpy, Copyright (c) 2018-2020 Per Gantelius, https://github.com/stuffmatic/fSpy
#   (GNU GPL v3 or later)
# which in turn follows E. Guillou, D. Meneveaux, E. Maisel, K. Bouatouch,
# "Using Vanishing Points for Camera Calibration and Coarse 3D Reconstruction
# from a Single Image" (2000).
#
# Additions compared to fSpy: any number (>= 2) of line segments per vanishing
# point (least squares), "parallel verticals" principal point modes (one of them
# keeps the camera exactly level), scaling by camera height, and a direct mapping
# to Blender camera settings for every sensor fit mode.

"""Pure-Python camera solver. Nothing in this module imports bpy, so it can be
tested outside Blender.

Coordinate frames used throughout
---------------------------------
relative     x in 0..1 from left to right, y in 0..1 from TOP to BOTTOM
             (same as fSpy). All user-facing control points use this frame.
image plane  origin at the image centre, y up, the LONGER image side spans
             [-1, 1]. Wide image: x in [-1, 1], y in [-1/aspect, 1/aspect].
             Tall image: x in [-aspect, aspect], y in [-1, 1].
camera       x right, y up, looking along -z. This is the local space of a
             Blender camera object, so no axis conversion is needed.

A camera-space point (x, y, z) projects to  pp + f * (x, y) / -z  in the image
plane, where pp is the principal point and f the "relative focal length"
(focal length divided by half of the longer sensor side).
"""

import math
from dataclasses import dataclass, field

DEFAULT_CAMERA_DISTANCE = 10.0   # same default as fSpy
_INFINITY = 1e7                  # image plane distance treated as "at infinity"

AXES = {
    '+X': (1.0, 0.0, 0.0), '-X': (-1.0, 0.0, 0.0),
    '+Y': (0.0, 1.0, 0.0), '-Y': (0.0, -1.0, 0.0),
    '+Z': (0.0, 0.0, 1.0), '-Z': (0.0, 0.0, -1.0),
}


# --------------------------------------------------------------------------
# Small vector / matrix helpers on tuples (no dependencies)
# --------------------------------------------------------------------------

def _sub2(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _add2(a, b):
    return (a[0] + b[0], a[1] + b[1])


def _mul2(a, s):
    return (a[0] * s, a[1] * s)


def _dot2(a, b):
    return a[0] * b[0] + a[1] * b[1]


def _cross2(a, b):
    return a[0] * b[1] - a[1] * b[0]


def _len2(a):
    return math.hypot(a[0], a[1])


def _norm2(a):
    n = _len2(a)
    return (a[0] / n, a[1] / n) if n > 0.0 else (0.0, 0.0)


def _dot3(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross3(a, b):
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def _len3(a):
    return math.sqrt(_dot3(a, a))


def _norm3(a):
    n = _len3(a)
    return (a[0] / n, a[1] / n, a[2] / n) if n > 0.0 else (0.0, 0.0, 0.0)


def _matvec3(m, v):
    return tuple(_dot3(row, v) for row in m)


def _matmul3(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3))
                 for i in range(3))


def _transpose3(m):
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


def _det3(m):
    return _dot3(m[0], _cross3(m[1], m[2]))


def _sym3_smallest_eigenvector(m):
    """Eigenvector of the smallest eigenvalue of a symmetric 3x3 matrix
    (cyclic Jacobi rotations)."""
    a = [list(row) for row in m]
    v = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    for _sweep in range(64):
        off = a[0][1] ** 2 + a[0][2] ** 2 + a[1][2] ** 2
        scale = a[0][0] ** 2 + a[1][1] ** 2 + a[2][2] ** 2
        if off <= 1e-30 * max(scale, 1e-300):
            break
        for p, q in ((0, 1), (0, 2), (1, 2)):
            apq = a[p][q]
            if apq == 0.0:
                continue
            theta = (a[q][q] - a[p][p]) / (2.0 * apq)
            t = math.copysign(1.0, theta) / (abs(theta) + math.sqrt(theta * theta + 1.0))
            c = 1.0 / math.sqrt(t * t + 1.0)
            s = t * c
            for k in range(3):
                akp, akq = a[k][p], a[k][q]
                a[k][p] = c * akp - s * akq
                a[k][q] = s * akp + c * akq
            for k in range(3):
                apk, aqk = a[p][k], a[q][k]
                a[p][k] = c * apk - s * aqk
                a[q][k] = s * apk + c * aqk
            for k in range(3):
                vkp, vkq = v[k][p], v[k][q]
                v[k][p] = c * vkp - s * vkq
                v[k][q] = s * vkp + c * vkq
    i = min(range(3), key=lambda k: a[k][k])
    return (v[0][i], v[1][i], v[2][i])


# --------------------------------------------------------------------------
# Coordinate frames
# --------------------------------------------------------------------------

def rel_to_plane(p, aspect):
    """Relative (0..1, y down) -> image plane (centred, y up)."""
    x, y = p
    if aspect <= 1.0:
        return ((-1.0 + 2.0 * x) * aspect, 1.0 - 2.0 * y)
    return (-1.0 + 2.0 * x, (1.0 - 2.0 * y) / aspect)


def plane_to_rel(p, aspect):
    """Image plane (centred, y up) -> relative (0..1, y down)."""
    x, y = p
    if aspect <= 1.0:
        return (0.5 * (x / aspect + 1.0), 0.5 * (1.0 - y))
    return (0.5 * (x + 1.0), 0.5 * (1.0 - y * aspect))


def plane_half_extent(aspect):
    """Half width and half height of the image in image plane units."""
    if aspect <= 1.0:
        return (aspect, 1.0)
    return (1.0, 1.0 / aspect)


# --------------------------------------------------------------------------
# Geometry building blocks
# --------------------------------------------------------------------------

def _line_through(p, q):
    """Homogeneous line through two points, normalised so that a^2 + b^2 = 1
    (then l . (x, y, 1) is the signed distance of (x, y) to the line)."""
    a = p[1] - q[1]
    b = q[0] - p[0]
    n = math.hypot(a, b)
    if n < 1e-12:
        return None
    c = -(a * p[0] + b * p[1])
    return (a / n, b / n, c / n)


def vanishing_point(segments):
    """Vanishing point (image plane) of two or more line segments
    (image plane). Two segments: their exact intersection. More segments:
    least-squares point in homogeneous coordinates, weighted by segment length.

    Returns (point, error). point is None when the lines are (nearly) parallel
    or a segment is degenerate."""
    if len(segments) < 2:
        return None, 'needs at least two lines'
    lines = []
    weights = []
    for seg in segments:
        line = _line_through(seg[0], seg[1])
        if line is None:
            return None, 'a line has zero length'
        lines.append(line)
        weights.append(_len2(_sub2(seg[1], seg[0])))

    if len(lines) == 2:
        h = _cross3(lines[0], lines[1])
    else:
        m = [[0.0] * 3 for _ in range(3)]
        for line, w in zip(lines, weights):
            for i in range(3):
                for j in range(3):
                    m[i][j] += w * line[i] * line[j]
        h = _sym3_smallest_eigenvector(m)

    planar = math.hypot(h[0], h[1])
    if abs(h[2]) * _INFINITY <= planar:
        return None, 'lines are parallel'
    return (h[0] / h[2], h[1] / h[2]), None


def lines_spread(segments):
    """Largest |sin| of the angle between any two segments (0 = all parallel)."""
    dirs = [_norm2(_sub2(s[1], s[0])) for s in segments]
    best = 0.0
    for i in range(len(dirs)):
        for j in range(i + 1, len(dirs)):
            best = max(best, abs(_cross2(dirs[i], dirs[j])))
    return best


def focal_from_vanishing_points(fu, fv, pp):
    """Relative focal length from two vanishing points of orthogonal directions
    and the principal point: (Fu - P).(Fv - P) + f^2 = 0.
    Returns None if the configuration is impossible."""
    f2 = -_dot2(_sub2(fu, pp), _sub2(fv, pp))
    if f2 <= 0.0:
        return None
    return math.sqrt(f2)


def orthocenter(a, b, c):
    """Orthocentre of triangle abc (None for a degenerate triangle)."""
    # (H - a).(b - c) = 0 and (H - b).(a - c) = 0
    d1 = _sub2(b, c)
    d2 = _sub2(a, c)
    det = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(det) < 1e-15:
        return None
    r1 = _dot2(a, d1)
    r2 = _dot2(b, d2)
    return ((r1 * d2[1] - r2 * d1[1]) / det, (d1[0] * r2 - d2[0] * r1) / det)


def project_onto_line(p, a, direction):
    """Orthogonal projection of p onto the line through a with the given direction."""
    d = _norm2(direction)
    t = _dot2(_sub2(p, a), d)
    return _add2(a, _mul2(d, t))


def level_vanishing_points(groups):
    """Vanishing points of two groups of segments (image plane) constrained to
    one HORIZONTAL line y = h of the image (a camera without tilt and roll).
    Least squares over all lines of both groups, weighted by segment length:
    (x1, x2, h) minimise  sum w (a x_k + b h + c)^2  with every line normalised
    (a^2 + b^2 = 1), i.e. the vanishing points are projected onto the common
    horizon that fits both groups best. Returns ((x1, h), (x2, h)) or None."""
    saa, sab, sac = [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]
    sbb = sbc = 0.0
    for k, segments in enumerate(groups):
        for p, q in segments:
            line = _line_through(p, q)
            if line is None:
                return None
            w = _len2(_sub2(q, p))
            a, b, c = line
            saa[k] += w * a * a
            sab[k] += w * a * b
            sac[k] += w * a * c
            sbb += w * b * b
            sbc += w * b * c
    if min(saa) < 1e-12:            # a group of horizontal lines: no point on the horizon
        return None
    den = sbb - sum(sab[k] * sab[k] / saa[k] for k in (0, 1))
    if den < 1e-12 * max(sbb, 1e-300):
        return None
    h = (sum(sab[k] * sac[k] / saa[k] for k in (0, 1)) - sbc) / den
    x = [-(sab[k] * h + sac[k]) / saa[k] for k in (0, 1)]
    return (x[0], h), (x[1], h)


def second_vanishing_point(fu, f, pp, horizon_dir):
    """fSpy 1-VP mode: the vanishing point Fv on the line through Fu with the
    given direction such that the directions to Fu and Fv are orthogonal."""
    fup = _sub2(fu, pp)
    if _len2(fup) < 1e-7:
        return None
    h = _norm2(horizon_dir)
    denom = _dot2(fup, h)
    if abs(denom) < 1e-12:
        return None
    k = -(_dot2(fup, fup) + f * f) / denom
    return _add2(_add2(fup, _mul2(h, k)), pp)


def rotation_from_vanishing_points(fu, fv, f, pp):
    """Matrix whose columns are the unit directions (camera space) towards
    Fu, Fv and their cross product (fSpy computeCameraRotationMatrix)."""
    u = _norm3((fu[0] - pp[0], fu[1] - pp[1], -f))
    v = _norm3((fv[0] - pp[0], fv[1] - pp[1], -f))
    w = _cross3(u, v)
    return ((u[0], v[0], w[0]),
            (u[1], v[1], w[1]),
            (u[2], v[2], w[2]))


def axis_assignment(axis1, axis2):
    """Rows: world directions of the 1st, 2nd and 3rd vanishing point axes."""
    r1 = AXES[axis1]
    r2 = AXES[axis2]
    r3 = _cross3(r1, r2)
    if _len3(r3) < 0.5:
        return None
    return (r1, r2, r3)


def axis_name(vec):
    for name, v in AXES.items():
        if _dot3(v, vec) > 0.99:
            return name
    return '?'


def project_direction(rotation_view, direction_world, f, pp):
    """Image plane vanishing point of a world direction (None = at infinity)."""
    d = _matvec3(rotation_view, direction_world)
    if abs(d[2]) * _INFINITY <= math.hypot(d[0], d[1]):
        return None
    return (pp[0] + f * d[0] / -d[2], pp[1] + f * d[1] / -d[2])


def _ray(p, f, pp):
    """Camera-space direction of the ray through image plane point p."""
    return ((p[0] - pp[0]) / f, (p[1] - pp[1]) / f, -1.0)


def _closest_point_on_first_line(p1, d1, p2, d2):
    """Point on line (p1 + s d1) closest to line (p2 + t d2)."""
    r = (p1[0] - p2[0], p1[1] - p2[1], p1[2] - p2[2])
    a = _dot3(d1, d1)
    b = _dot3(d1, d2)
    c = _dot3(d2, d2)
    d = _dot3(d1, r)
    e = _dot3(d2, r)
    den = a * c - b * b
    if abs(den) < 1e-15:
        return None
    s = (b * e - c * d) / den
    return (p1[0] + s * d1[0], p1[1] + s * d1[1], p1[2] + s * d1[2])


# --------------------------------------------------------------------------
# Solver input / output
# --------------------------------------------------------------------------

@dataclass
class Settings:
    mode: str = '2VP'               # '1VP' or '2VP'
    # 'CENTER'    principal point at the image centre
    # 'MANUAL'    principal point at control point `principal_point`
    # 'THIRD_VP'  orthocentre of the three vanishing points (2VP only)
    # 'VERTICALS' parallel verticals: principal point on the horizon line,
    #             at the projection of control point `principal_point`
    # 'LEVEL'     parallel verticals with a LEVEL camera (2VP only): both vanishing
    #             points on one horizontal image line (least squares), principal
    #             point on it below / above control point `principal_point`.
    #             No tilt and no roll: the third axis is exactly the camera's up.
    pp_mode: str = 'CENTER'
    vp1_axis: str = '+X'
    vp2_axis: str = '+Y'
    focal_rel: float = 1.0          # 1VP only, see focal_rel_from_blender()
    scale_mode: str = 'NONE'        # 'NONE', 'DISTANCE' or 'CAMERA_HEIGHT'
    reference_axis: str = 'Z'       # 'X', 'Y' or 'Z' (DISTANCE and CAMERA_HEIGHT)
    reference_distance: float = 1.0
    camera_height: float = 1.6
    camera_distance: float = DEFAULT_CAMERA_DISTANCE
    # names of the three vanishing points in messages (e.g. 'X lines')
    labels: tuple = ('VP1', 'VP2', 'VP3')


def _default_segments():
    return []


@dataclass
class ControlPoints:
    """All points in the relative frame (0..1, y down)."""
    vp1: list = field(default_factory=_default_segments)   # [((x, y), (x, y)), ...]
    vp2: list = field(default_factory=_default_segments)
    vp3: list = field(default_factory=_default_segments)   # pp_mode THIRD_VP only
    horizon: tuple = ((0.2, 0.5), (0.8, 0.5))              # 1VP only, direction
    principal_point: tuple = (0.5, 0.5)
    origin: tuple = (0.5, 0.5)
    # Scale mode DISTANCE: the 3D distance between the two `reference` points
    # equals Settings.reference_distance. Both points are taken on a line
    # parallel to the reference axis. That line passes through the point where
    # the ray through `reference_anchor` meets the plane through the world
    # origin perpendicular to the reference axis. Anchor None = the origin
    # control point, i.e. the line passes through the world origin (fSpy default).
    reference: tuple = ((0.5, 0.5), (0.5, 0.4))
    reference_anchor: tuple = None


@dataclass
class Solution:
    ok: bool = False
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    aspect: float = 1.0
    focal_rel: float = 0.0
    principal_point: tuple = (0.0, 0.0)          # image plane
    vanishing_points: tuple = (None, None, None)  # image plane, None = infinity
    vanishing_point_axes: tuple = ('+X', '+Y', '+Z')
    view_rotation: tuple = None    # 3x3 world -> camera
    camera_rotation: tuple = None  # 3x3 camera -> world
    camera_location: tuple = (0.0, 0.0, 0.0)
    view_translation: tuple = (0.0, 0.0, 0.0)    # world origin in camera space

    @property
    def matrix_world(self):
        """4x4 camera -> world matrix as nested tuples (rows)."""
        r = self.camera_rotation
        c = self.camera_location
        return ((r[0][0], r[0][1], r[0][2], c[0]),
                (r[1][0], r[1][1], r[1][2], c[1]),
                (r[2][0], r[2][1], r[2][2], c[2]),
                (0.0, 0.0, 0.0, 1.0))

    def project(self, point_world):
        """World point -> image plane (None if behind the camera)."""
        p = _matvec3(self.view_rotation, point_world)
        p = (p[0] + self.view_translation[0],
             p[1] + self.view_translation[1],
             p[2] + self.view_translation[2])
        if p[2] >= -1e-12:
            return None
        f = self.focal_rel
        return (self.principal_point[0] + f * p[0] / -p[2],
                self.principal_point[1] + f * p[1] / -p[2])

    def project_rel(self, point_world):
        p = self.project(point_world)
        return None if p is None else plane_to_rel(p, self.aspect)

    def horizon(self, up=(0.0, 0.0, 1.0)):
        """Horizon line in the image plane as (point, direction), or None when the
        camera looks straight up or down (no horizon in the image).

        With up_cam = view_rotation . up = (a, b, c), a ray through image point
        (x, y) is horizontal when a (x - pp.x) + b (y - pp.y) = c f."""
        if not self.ok:
            return None
        a, b, c = _matvec3(self.view_rotation, up)
        n2 = a * a + b * b
        if n2 < 1e-12:
            return None
        k = c * self.focal_rel / n2
        point = (self.principal_point[0] + k * a, self.principal_point[1] + k * b)
        return point, (-b, a)

    def horizon_rel(self, up=(0.0, 0.0, 1.0)):
        """Two points of the horizon line in the relative frame (or None)."""
        h = self.horizon(up)
        if h is None:
            return None
        p, d = h
        return (plane_to_rel(p, self.aspect),
                plane_to_rel((p[0] + d[0], p[1] + d[1]), self.aspect))

    def vanishing_points_rel(self):
        return tuple(None if v is None else plane_to_rel(v, self.aspect)
                     for v in self.vanishing_points)


def _segments_to_plane(segments, aspect):
    return [(rel_to_plane(s[0], aspect), rel_to_plane(s[1], aspect)) for s in segments]


def _vp_from_rel(segments, aspect, label, sol):
    seg_plane = _segments_to_plane(segments, aspect)
    vp, err = vanishing_point(seg_plane)
    if vp is None:
        sol.errors.append(f'{label}: {err}')
        return None
    if len(seg_plane) >= 2 and lines_spread(seg_plane) < 0.01:
        sol.warnings.append(f'{label}: lines are nearly parallel, low accuracy')
    return vp


def solve(points, settings, aspect):
    """Compute the camera for the given control points and settings.
    aspect = image width / image height."""
    sol = Solution(aspect=aspect)
    if not aspect or aspect <= 0.0:
        sol.errors.append('invalid image aspect ratio')
        return sol

    axes = axis_assignment(settings.vp1_axis, settings.vp2_axis)
    if axes is None:
        sol.errors.append('the two vanishing points must use different axes')
        return sol
    sol.vanishing_point_axes = (settings.vp1_axis, settings.vp2_axis, axis_name(axes[2]))

    L1, L2, L3 = settings.labels
    fu = _vp_from_rel(points.vp1, aspect, L1, sol)
    if fu is None:
        return sol

    # Principal point (before any horizon constraint)
    pp_control = rel_to_plane(points.principal_point, aspect)
    if settings.pp_mode in ('MANUAL', 'VERTICALS', 'LEVEL'):
        pp = pp_control
    else:
        pp = (0.0, 0.0)

    if settings.mode == '1VP':
        f = settings.focal_rel
        if not f or f <= 0.0:
            sol.errors.append('focal length must be positive')
            return sol
        h0 = rel_to_plane(points.horizon[0], aspect)
        h1 = rel_to_plane(points.horizon[1], aspect)
        hdir = _sub2(h1, h0)
        if _len2(hdir) < 1e-9:
            sol.errors.append('horizon line has zero length')
            return sol
        if settings.pp_mode == 'VERTICALS':
            pp = project_onto_line(pp_control, fu, hdir)
        elif settings.pp_mode in ('THIRD_VP', 'LEVEL'):
            sol.errors.append(f'principal point mode {settings.pp_mode} needs 2VP mode')
            return sol
        fv = second_vanishing_point(fu, f, pp, hdir)
        if fv is None:
            sol.errors.append('cannot place the second vanishing point '
                              f'({L1} point at the principal point or the horizon points at it)')
            return sol
    elif settings.mode == '2VP':
        fv = _vp_from_rel(points.vp2, aspect, L2, sol)
        if fv is None:
            return sol
        if settings.pp_mode == 'THIRD_VP':
            fw = _vp_from_rel(points.vp3, aspect, L3, sol)
            if fw is None:
                return sol
            pp = orthocenter(fu, fv, fw)
            if pp is None:
                sol.errors.append('the three vanishing points lie on one line')
                return sol
        elif settings.pp_mode == 'VERTICALS':
            if _len2(_sub2(fv, fu)) < 1e-9:
                sol.errors.append(f'the vanishing points of {L1} and {L2} coincide')
                return sol
            pp = project_onto_line(pp_control, fu, _sub2(fv, fu))
        elif settings.pp_mode == 'LEVEL':
            level = level_vanishing_points([_segments_to_plane(points.vp1, aspect),
                                            _segments_to_plane(points.vp2, aspect)])
            if level is None:
                sol.errors.append(f'{L1} and {L2}: no level horizon fits these lines')
                return sol
            fu, fv = level
            pp = (pp_control[0], fu[1])
        f = focal_from_vanishing_points(fu, fv, pp)
        if f is None:
            sol.errors.append(f'impossible configuration: the vanishing points of {L1} and {L2} '
                              'must lie on opposite sides of the principal point')
            return sol
    else:
        sol.errors.append(f'unknown mode {settings.mode!r}')
        return sol

    m = rotation_from_vanishing_points(fu, fv, f, pp)
    det = _det3(m)
    if abs(det - 1.0) > 1e-6:
        sol.errors.append(f'invalid vanishing point configuration (rotation determinant {det:.5f})')
        return sol

    view_rot = _matmul3(m, axes)          # world -> camera
    cam_rot = _transpose3(view_rot)       # camera -> world

    sol.focal_rel = f
    sol.principal_point = pp
    sol.view_rotation = view_rot
    sol.camera_rotation = cam_rot
    sol.vanishing_points = (fu, fv, project_direction(view_rot, axes[2], f, pp))

    if 2.0 * math.atan(1.0 / f) < math.radians(1.0):
        sol.warnings.append('extremely narrow field of view, result is unreliable')

    # Translation: world origin on the ray through the origin control point
    o = rel_to_plane(points.origin, aspect)
    dist = settings.camera_distance if settings.camera_distance > 0 else DEFAULT_CAMERA_DISTANCE
    t = tuple(dist * c for c in _ray(o, f, pp))

    scale = 1.0
    if settings.scale_mode == 'DISTANCE':
        axis_world = AXES['+' + settings.reference_axis]
        axis_cam = _matvec3(view_rot, axis_world)
        anchor_3d = t
        if points.reference_anchor is not None:
            ray_a = _ray(rel_to_plane(points.reference_anchor, aspect), f, pp)
            den = _dot3(ray_a, axis_cam)
            if abs(den) < 1e-12:
                anchor_3d = None
            else:
                anchor_3d = tuple(c * _dot3(t, axis_cam) / den for c in ray_a)
        handles = []
        for p_rel in points.reference:
            ray = _ray(rel_to_plane(p_rel, aspect), f, pp)
            q = None
            if anchor_3d is not None:
                q = _closest_point_on_first_line((0.0, 0.0, 0.0), ray, anchor_3d, axis_cam)
            handles.append(q)
        if None in handles:
            sol.warnings.append('reference distance: the reference axis points at the camera')
        else:
            d = _len3(tuple(a - b for a, b in zip(handles[0], handles[1])))
            if d < 1e-12:
                sol.warnings.append('reference distance: the two points coincide')
            else:
                scale = settings.reference_distance / d
    elif settings.scale_mode == 'CAMERA_HEIGHT':
        axis_world = AXES['+' + settings.reference_axis]
        loc = _matvec3(cam_rot, t)
        height = -_dot3(loc, axis_world)
        if height <= 1e-12:
            sol.warnings.append('camera height: the camera is not above the origin plane '
                                '(the origin point must be below the horizon)')
        else:
            scale = settings.camera_height / height

    t = tuple(c * scale for c in t)
    sol.view_translation = t
    loc = _matvec3(cam_rot, t)
    sol.camera_location = (-loc[0], -loc[1], -loc[2])
    sol.ok = True
    return sol


# --------------------------------------------------------------------------
# Mapping to / from Blender camera settings (no bpy needed)
# --------------------------------------------------------------------------

def _fit_half_extent(aspect, sensor_fit):
    """(effective fit, half extent of the fitted side in image plane units)."""
    if sensor_fit == 'AUTO':
        fit = 'HORIZONTAL' if aspect >= 1.0 else 'VERTICAL'
    else:
        fit = sensor_fit
    hx, hy = plane_half_extent(aspect)
    return fit, (hx if fit == 'HORIZONTAL' else hy)


def _sensor_size(sensor_fit, sensor_width, sensor_height):
    # AUTO and HORIZONTAL use sensor_width, VERTICAL uses sensor_height
    return sensor_height if sensor_fit == 'VERTICAL' else sensor_width


def blender_camera_settings(sol, sensor_fit='AUTO', sensor_width=36.0, sensor_height=24.0):
    """lens (mm), shift_x, shift_y and matrix_world for a Blender camera.
    The render aspect (resolution x pixel aspect) must equal sol.aspect.

    Blender measures shift in units of the side the sensor is fitted to
    (AUTO: the longer side) - verified with Camera.view_frame in Blender 5.2."""
    _fit, s = _fit_half_extent(sol.aspect, sensor_fit)
    size = _sensor_size(sensor_fit, sensor_width, sensor_height)
    return {
        'lens': sol.focal_rel / s * size / 2.0,
        'shift_x': -sol.principal_point[0] / (2.0 * s),
        'shift_y': -sol.principal_point[1] / (2.0 * s),
        'matrix_world': sol.matrix_world,
    }


def focal_rel_from_blender(lens, aspect, sensor_fit='AUTO', sensor_width=36.0, sensor_height=24.0):
    """Relative focal length (image plane units) of a Blender lens setting."""
    _fit, s = _fit_half_extent(aspect, sensor_fit)
    size = _sensor_size(sensor_fit, sensor_width, sensor_height)
    return lens * 2.0 * s / size
