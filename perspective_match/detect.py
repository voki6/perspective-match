# SPDX-License-Identifier: GPL-3.0-or-later
"""Detect Lines: straight edges on the photo -> vanishing points
-> lines and a mode. numpy only, no bpy: tested by tests/test_detect.py.

detect(pixels) takes the photo as an array (rows from the TOP, values 0..1 or
0..255, gray or RGB(A)) and returns a Detection: the chosen mode, the groups
of segments (relative coordinates, as the stored lines) and why.

Steps
  segments    blur, Sobel gradients; pixels with a clear gradient are joined
              into "line support regions" (8-neighbours with the same gradient
              direction, two direction partitions offset by half a bin, the
              larger region of each pixel wins); a region that is long and
              thin is a segment (PCA through it). Vectorised union-find.
  vanishing   RANSAC over pairs of segments (score: total length of the
              segments pointing at the point), refined by reweighted least
              squares in homogeneous coordinates, so points at infinity
              (parallel lines) are handled the same way. Up to four groups.
  uncertainty each group is fitted twice: free and at infinity; the
              difference of the residuals against their spread (an F-like
              test) says whether the lines are parallel within their error.
              The principal point of three vanishing points (orthocentre) is
              bootstrapped to tell whether it differs from the centre.
  mode        3 Points / Parallel Verticals / 2 Points / 1 Point (see choose()).
"""

import math

import numpy as np

try:
    from . import model as M
except ImportError:          # imported as a top level module by the tests
    import model as M

WORK_SIZE = 1200             # longer side the photo is analysed at (pixels)
ANGLE_BINS = 16              # gradient direction bins over 360 degrees
MIN_GRADIENT = 0.012         # weakest edge (0..1 brightness per pixel)
MAX_WIDTH = 5.0              # widest line support region (pixels)
MIN_LENGTH = 0.02            # shortest segment, share of the longer side
INLIER_ANGLE = math.radians(1.5)
CANDIDATE_PAIRS = 400        # RANSAC hypotheses (pairs of segments) per candidate
MAX_CANDIDATES = 6           # vanishing points the Manhattan frame is chosen from
MIN_SUPPORT = 0.25           # image plane: least total length of a direction
SQUARE_TOL = 3.0             # degrees: directions taken as square to each other
NOISE_FLOOR = 0.3            # pixels: least endpoint noise assumed for a segment
PARALLEL_F = 6.63            # chi-square(1), 99 %: still parallel within the error
ORTHO_D2 = 13.8              # chi-square(2), 99.9 %: principal point is not the centre
PP_SYSTEMATIC = 0.08         # image plane: principal point error the bootstrap does not
                             # see (lens distortion, edge bias): about 4 % of the width,
                             # measured on real photos (orthocentres 0.1-0.15 off for
                             # unshifted cameras), so 3 Points needs a clearly shifted one
BOOTSTRAP = 40
F_RANGE = (0.3, 30.0)        # plausible relative focal length (about 5..540 mm)
MAX_F_SPREAD = 0.05          # focal length must be known within 5 % (bootstrap)


# --------------------------------------------------------------------------
# Pixels
# --------------------------------------------------------------------------

def to_gray(pixels):
    """2D float32 brightness 0..1 from gray / RGB / RGBA (rows from the top)."""
    a = np.asarray(pixels)
    scale = 1.0 / 255.0 if a.dtype == np.uint8 else 1.0
    a = a.astype(np.float32, copy=False)
    if a.ndim == 3:
        a = a[..., 0] * 0.299 + a[..., 1] * 0.587 + a[..., 2] * 0.114
    return a * scale if scale != 1.0 else a


def downscale(gray, size=WORK_SIZE):
    """Block mean by a whole factor so the longer side is at most about `size`
    (Blender scales the photo itself before; this is for plain arrays)."""
    k = int(math.ceil(max(gray.shape) / float(size)))
    if k <= 1:
        return gray
    h, w = gray.shape[0] // k * k, gray.shape[1] // k * k
    return gray[:h, :w].reshape(h // k, k, w // k, k).mean(axis=(1, 3))


# --------------------------------------------------------------------------
# Segments
# --------------------------------------------------------------------------

def _blur(g):
    k = np.array([1.0, 4.0, 6.0, 4.0, 1.0], np.float32) / 16.0
    p = np.pad(g, 2, mode='edge')
    t = sum(k[i] * p[:, i:i + g.shape[1]] for i in range(5))
    return sum(k[i] * t[i:i + g.shape[0], :] for i in range(5))


def _gradients(g):
    p = np.pad(g, 1, mode='edge')
    gx = ((p[:-2, 2:] + 2 * p[1:-1, 2:] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[1:-1, :-2] + p[2:, :-2])) / 8
    gy = ((p[2:, :-2] + 2 * p[2:, 1:-1] + p[2:, 2:]) - (p[:-2, :-2] + 2 * p[:-2, 1:-1] + p[:-2, 2:])) / 8
    return gx, gy


_OFFSETS = ((0, 1), (1, 0), (1, 1), (1, -1))


def _components(mask, bins, node):
    """Labels (0..k-1, per masked pixel) of 8-connected regions of equal bin."""
    H, W = mask.shape
    us, vs = [], []
    for dy, dx in _OFFSETS:
        y0, y1 = slice(0, H - dy), slice(dy, H)
        x0, x1 = (slice(0, W - dx), slice(dx, W)) if dx >= 0 else (slice(-dx, W), slice(0, W + dx))
        m = mask[y0, x0] & mask[y1, x1] & (bins[y0, x0] == bins[y1, x1])
        us.append(node[y0, x0][m])
        vs.append(node[y1, x1][m])
    u, v = np.concatenate(us), np.concatenate(vs)
    parent = np.arange(int(mask.sum()), dtype=np.int32)
    while True:
        pu, pv = parent[u], parent[v]
        diff = pu != pv
        if not diff.any():
            break
        u, v, pu, pv = u[diff], v[diff], pu[diff], pv[diff]
        np.minimum.at(parent, np.maximum(pu, pv), np.minimum(pu, pv))
        while True:                                  # point every node at its root
            pp = parent[parent]
            if np.array_equal(pp, parent):
                break
            parent = pp
    return np.unique(parent, return_inverse=True)[1]


def segments(gray):
    """Straight segments of a gray image: (p1, p2) pixel arrays (x right,
    y down, pixel centres at +0.5) and their widths, longest first."""
    g = _blur(gray)
    gx, gy = _gradients(g)
    mag = np.hypot(gx, gy)
    noise = float(np.median(mag)) * 1.4826
    mask = mag > max(MIN_GRADIENT, 3.0 * noise)
    n = int(mask.sum())
    empty = (np.zeros((0, 2)), np.zeros((0, 2)), np.zeros(0))
    if n < 20:
        return empty
    H, W = gray.shape
    node = np.full(mask.shape, -1, np.int32)
    idx = np.flatnonzero(mask)
    node.flat[idx] = np.arange(n, dtype=np.int32)
    ang = np.arctan2(gy, gx)
    ang *= ANGLE_BINS / (2 * math.pi)
    del g, gx, gy
    labels, sizes = [], []
    for shift in (0.0, 0.5):                         # two partitions, half a bin apart
        bins = (np.floor(ang + shift).astype(np.int8) % ANGLE_BINS).astype(np.int8)
        lab = _components(mask, bins, node)
        del bins
        labels.append(lab)
        sizes.append(np.bincount(lab))
    del ang, node
    # each pixel belongs to the larger of its two regions
    a_wins = sizes[0][labels[0]] >= sizes[1][labels[1]]
    ys, xs = np.divmod(idx, W)
    x = xs.astype(np.float64) + 0.5
    y = ys.astype(np.float64) + 0.5
    w = mag.flat[idx].astype(np.float64)
    min_len = MIN_LENGTH * max(H, W)
    out_p1, out_p2, out_w = [], [], []
    for lab, size, mine in ((labels[0], sizes[0], a_wins), (labels[1], sizes[1], ~a_wins)):
        keep = np.bincount(lab[mine], minlength=len(size)) * 2 >= size
        keep &= size >= max(8, int(min_len))
        sel = keep[lab]
        if not sel.any():
            continue
        comp = np.unique(lab[sel], return_inverse=True)[1]
        k = comp.max() + 1
        xs_, ys_, ws = x[sel], y[sel], w[sel]
        sw = np.bincount(comp, ws, k)
        cx, cy = np.bincount(comp, ws * xs_, k) / sw, np.bincount(comp, ws * ys_, k) / sw
        dx, dy = xs_ - cx[comp], ys_ - cy[comp]
        cxx = np.bincount(comp, ws * dx * dx, k) / sw
        cyy = np.bincount(comp, ws * dy * dy, k) / sw
        cxy = np.bincount(comp, ws * dx * dy, k) / sw
        theta = 0.5 * np.arctan2(2 * cxy, cxx - cyy)
        root = np.sqrt(0.25 * (cxx - cyy) ** 2 + cxy ** 2)
        minor = np.maximum(0.5 * (cxx + cyy) - root, 0.0)
        c, s = np.cos(theta), np.sin(theta)
        t = dx * c[comp] + dy * s[comp]
        tmin, tmax = np.full(k, np.inf), np.full(k, -np.inf)
        np.minimum.at(tmin, comp, t)
        np.maximum.at(tmax, comp, t)
        length = tmax - tmin
        width = np.sqrt(12.0 * minor)
        good = (length >= min_len) & (width <= MAX_WIDTH) & (length >= 6.0 * np.maximum(width, 1.0))
        p1 = np.stack([cx + tmin * c, cy + tmin * s], 1)[good]
        p2 = np.stack([cx + tmax * c, cy + tmax * s], 1)[good]
        out_p1.append(p1)
        out_p2.append(p2)
        out_w.append(width[good])
    if not out_p1:
        return empty
    p1, p2, wd = np.concatenate(out_p1), np.concatenate(out_p2), np.concatenate(out_w)
    order = np.argsort(-np.hypot(*(p2 - p1).T))
    return p1[order], p2[order], wd[order]


# --------------------------------------------------------------------------
# Vanishing points
# --------------------------------------------------------------------------

class Segs:
    """Segments on the image plane (centred, y up, longer side spans [-1, 1])."""

    def __init__(self, p1_px, p2_px, w, h):
        self.w, self.h = w, h
        self.half = max(w, h) / 2.0
        to = lambda p: np.stack([(p[:, 0] - w / 2) / self.half, -(p[:, 1] - h / 2) / self.half], 1)
        self.p1, self.p2 = to(p1_px), to(p2_px)
        d = self.p2 - self.p1
        self.L = np.hypot(d[:, 0], d[:, 1])
        self.D = d / self.L[:, None]
        self.M = 0.5 * (self.p1 + self.p2)
        h1 = np.c_[self.p1, np.ones(len(self.L))]
        h2 = np.c_[self.p2, np.ones(len(self.L))]
        lines = np.cross(h1, h2)
        self.lines = lines / np.hypot(lines[:, 0], lines[:, 1])[:, None]

    def __len__(self):
        return len(self.L)

    def rel(self, idx):
        """[(a, b), ...] relative 0..1, y down, as the stored lines."""
        f = lambda p: (float(p[0] * self.half / self.w + 0.5), float(0.5 - p[1] * self.half / self.h))
        return [(f(self.p1[i]), f(self.p2[i])) for i in idx]


def _sin_to(segs, v, idx=None):
    """|sin| of the angle between each segment and the direction from its
    middle to the vanishing point v (homogeneous), and whether v lies beyond
    the segment (not on it)."""
    D, M, L = (segs.D, segs.M, segs.L) if idx is None else (segs.D[idx], segs.M[idx], segs.L[idx])
    e = v[..., None, :2] - M * v[..., None, 2:3]
    ne = np.hypot(e[..., 0], e[..., 1])
    s = np.abs(D[..., 0] * e[..., 1] - D[..., 1] * e[..., 0]) / np.maximum(ne, 1e-300)
    beyond = ne > np.abs(v[..., None, 2]) * L * 0.5
    return s, beyond, ne


def _fit(segs, idx, v=None, infinite=False, rounds=5):
    """Vanishing point (unit homogeneous 3-vector) of the segments idx by
    least squares of their angles (reweighted, works at infinity)."""
    lines, L = segs.lines[idx], segs.L[idx]
    for _ in range(rounds):
        if v is None:
            wgt = L * L
        else:
            _s, _b, ne = _sin_to(segs, v, idx)
            wgt = L * L / np.maximum(ne * ne, 1e-30)
            wgt /= wgt.max()
        if infinite:
            a = lines[:, :2]
            m = (a * wgt[:, None]).T @ a
            vec = np.linalg.eigh(m)[1][:, 0]
            nv = np.array([vec[0], vec[1], 0.0])
        else:
            m = (lines * wgt[:, None]).T @ lines
            nv = np.linalg.eigh(m)[1][:, 0]
        if v is not None and np.dot(nv, v) < 0:
            nv = -nv
        v = nv / np.linalg.norm(nv)
    return v


def _rss(segs, idx, v):
    s, _b, _n = _sin_to(segs, v, idx)
    return float(np.sum((0.5 * segs.L[idx] * s) ** 2))


class Group:
    """Segments pointing at one vanishing point."""

    def __init__(self, segs, idx):
        self.idx = idx
        self.v = _fit(segs, idx)
        self.support = float(segs.L[idx].sum())
        n = len(idx)
        rss = _rss(segs, idx, self.v)
        v_inf = _fit(segs, idx, infinite=True)
        rss_inf = _rss(segs, idx, v_inf)
        floor = (NOISE_FLOOR / segs.half) ** 2
        self.sigma2 = max(rss / max(n - 2, 1), floor)
        self.parallel_stat = (rss_inf - rss) / self.sigma2
        self.parallel = self.parallel_stat < PARALLEL_F
        # mean direction of the segments (doubled angles), from the image vertical
        ang = 2.0 * np.arctan2(segs.D[idx, 1], segs.D[idx, 0])
        wl = segs.L[idx]
        mean = 0.5 * math.atan2(float(np.sum(wl * np.sin(ang))), float(np.sum(wl * np.cos(ang))))
        self.tilt = abs(abs(math.degrees(mean)) - 90.0)      # 0 = vertical on the photo
        self.vertical = self.tilt < 45.0
        self.role = None

    @property
    def point(self):
        """Finite vanishing point (image plane) or None."""
        return None if abs(self.v[2]) < 1e-12 else self.v[:2] / self.v[2]


def _inliers(segs, vs):
    """Boolean (len(vs), len(segs)): segment points at the vanishing point."""
    out = []
    for k in range(0, len(vs), 200):                     # in chunks: bounded memory
        s, beyond, _ne = _sin_to(segs, vs[k:k + 200])
        out.append((s < math.sin(INLIER_ANGLE)) & beyond)
    return np.concatenate(out) if out else np.zeros((0, len(segs)), bool)


def candidates(segs, rng):
    """Up to MAX_CANDIDATES vanishing points (Group), strongest first: RANSAC
    over pairs of segments, refined, its segments taken out, again. Which of
    them make the building's three square directions is decided afterwards
    (frames), and every segment is assigned again then."""
    free = np.arange(len(segs))
    out = []
    while len(out) < MAX_CANDIDATES and len(free) >= 2:
        L = segs.L[free]
        p = L / L.sum()
        i = rng.choice(len(free), CANDIDATE_PAIRS, p=p)
        j = rng.choice(len(free), CANDIDATE_PAIRS, p=p)
        i, j = free[i[i != j]], free[j[i != j]]
        hyp = np.cross(segs.lines[i], segs.lines[j])
        nh = np.linalg.norm(hyp, axis=1)
        hyp = hyp[nh > 1e-12] / nh[nh > 1e-12][:, None]
        if not len(hyp):
            break
        inl = np.concatenate([(_sin_to(segs, hyp[k:k + 200], free)[0] < math.sin(INLIER_ANGLE)) &
                              _sin_to(segs, hyp[k:k + 200], free)[1]
                              for k in range(0, len(hyp), 200)])
        best = int(np.argmax(inl.astype(np.float64) @ L))
        v, members = hyp[best], free[inl[best]]
        for _ in range(2):                                   # refine and collect again
            if len(members) < 2:
                break
            v = _fit(segs, members, v)
            s, beyond, _ne = _sin_to(segs, v, free)
            members = free[(s < math.sin(INLIER_ANGLE)) & beyond]
        if len(members) < 3 or segs.L[members].sum() < MIN_SUPPORT:
            break
        out.append(Group(segs, members))
        free = np.setdiff1d(free, members)
    return out


def _direction(v, f, pp):
    """3D direction (camera space) of the homogeneous vanishing point v."""
    d = np.array([v[0] - pp[0] * v[2], v[1] - pp[1] * v[2], -f * v[2]])
    return d / np.linalg.norm(d)


def _point(d, f, pp):
    """Homogeneous vanishing point of the camera-space direction d."""
    v = np.array([d[0] - pp[0] * d[2] / f, d[1] - pp[1] * d[2] / f, -d[2] / f])
    return v / np.linalg.norm(v)


def _square(d1, d2):
    """Degrees by which two directions are not perpendicular."""
    return math.degrees(math.asin(min(1.0, abs(float(d1 @ d2)))))


def frames(cands):
    """Manhattan frames (three square directions) from the candidates: the
    homogeneous vanishing points of the three directions (one may be
    predicted, with no candidate of its own)."""
    fin = [g for g in cands if not g.parallel and g.point is not None]
    par = [g for g in cands if g.parallel or g.point is None]
    centre = np.zeros(2)
    out = []
    # two finite points, principal point at the centre: the third is predicted
    for i, a in enumerate(fin):
        for b in fin[i + 1:]:
            f2 = _focal2(a.point, b.point, centre)
            if _plausible_f(f2):
                f = math.sqrt(f2)
                dc = np.cross(_direction(a.v, f, centre), _direction(b.v, f, centre))
                out.append((a.v, b.v, _point(dc, f, centre)))
    # three finite points: principal point at their orthocentre (3 Points)
    for i, a in enumerate(fin):
        for j, b in enumerate(fin[i + 1:], i + 1):
            for c in fin[j + 1:]:
                o = _orthocentre(a.point, b.point, c.point)
                if o is not None and np.hypot(*o) <= 0.5 and \
                        _plausible_f(_focal2(a.point, b.point, o)):
                    out.append((a.v, b.v, c.v))
    # parallel lines (a level camera's verticals) and two finite points whose
    # horizon is square to them; principal point on that horizon, centre column
    for u in par:
        ud = np.array([u.v[0], u.v[1]]) / np.hypot(u.v[0], u.v[1])
        for i, b in enumerate(fin):
            for c in fin[i + 1:]:
                hl = np.cross(b.v, c.v)
                hd = np.array([hl[1], -hl[0]]) / max(np.hypot(hl[0], hl[1]), 1e-300)
                if math.degrees(math.asin(min(1.0, abs(float(hd @ ud))))) > SQUARE_TOL:
                    continue
                column = np.array([-ud[1], ud[0], 0.0])      # through the centre, along u
                x = np.cross(hl, column)
                pp = x[:2] / x[2] if abs(x[2]) > 1e-12 else centre
                if _plausible_f(_focal2(b.point, c.point, pp)):
                    out.append((u.v, b.v, c.v))
    # one finite point and two sets of lines square to each other on the photo
    for b in fin:
        for i, u1 in enumerate(par):
            for u2 in par[i + 1:]:
                d1 = np.array([u1.v[0], u1.v[1], 0.0])
                d2 = np.array([u2.v[0], u2.v[1], 0.0])
                if _square(d1 / np.linalg.norm(d1), d2 / np.linalg.norm(d2)) <= SQUARE_TOL:
                    out.append((b.v, u1.v, u2.v))
    return out


def manhattan_groups(segs, rng):
    """Groups of the best Manhattan frame: the one whose three directions
    take the most segments (length), each segment counted once. Lines in
    other directions (a roof at 60 degrees, a canopy, curves) are left out
    instead of being taken for a direction of the building."""
    cands = candidates(segs, rng)
    best, best_score, best_assign = None, 0.0, None
    for fr in frames(cands):
        vs = np.array(fr)
        s, beyond, _ne = _sin_to(segs, vs)
        s = np.where(beyond, s, np.inf)
        k = np.argmin(s, axis=0)
        ok = s[k, np.arange(len(segs))] < math.sin(INLIER_ANGLE)
        score = float(segs.L[ok].sum())
        if score > best_score:
            best, best_score, best_assign = fr, score, (k, ok)
    if best is None:
        return cands                           # no square frame: let choose() say why
    k, ok = best_assign
    groups = []
    for n in range(3):
        idx = np.flatnonzero(ok & (k == n))
        if len(idx) >= 3 and segs.L[idx].sum() >= MIN_SUPPORT:
            groups.append(Group(segs, idx))
    groups.sort(key=lambda g: -g.support)
    return groups


def _orthocentre(a, b, c):
    """Orthocentre of the triangle a, b, c (image plane) or None."""
    m = np.array([b - c, a - c])
    r = np.array([np.dot(a, b - c), np.dot(b, a - c)])
    if abs(np.linalg.det(m)) < 1e-12:
        return None
    return np.linalg.solve(m, r)


def _focal2(a, b, pp):
    return -float(np.dot(a - pp, b - pp))


def _plausible_f(f2):
    return f2 > 0 and F_RANGE[0] <= math.sqrt(f2) <= F_RANGE[1]


def _bootstrap_points(segs, groups, rng, rounds=BOOTSTRAP):
    """Vanishing points of the groups refitted on resampled segments."""
    for _ in range(rounds):
        out = []
        for g in groups:
            pick = rng.choice(g.idx, len(g.idx))
            v = _fit(segs, pick, g.v, rounds=2)
            out.append(None if abs(v[2]) < 1e-12 else v[:2] / v[2])
        yield out


def pair_spread(segs, a, b, rng, level=False):
    """Relative spread of the focal length two vanishing points give (principal
    point at the centre; level: on the horizon, only x counts), bootstrapped:
    smaller = more reliable pair."""
    fs = []
    for pa, pb in _bootstrap_points(segs, (a, b), rng):
        if pa is not None and pb is not None:
            f2 = -float(pa[0] * pb[0]) if level else _focal2(pa, pb, np.zeros(2))
            if f2 > 0:
                fs.append(math.sqrt(f2))
    if len(fs) < BOOTSTRAP // 2:
        return math.inf
    return float(np.std(fs) / np.mean(fs))


def principal_point_test(segs, groups, rng):
    """(orthocentre, Mahalanobis distance² from the centre) of three finite
    vanishing points, bootstrapped over their segments; None if not a valid
    acute triangle."""
    pts = [g.point for g in groups]
    if any(p is None for p in pts):
        return None
    o = _orthocentre(*pts)
    if o is None or not _plausible_f(_focal2(pts[0], pts[1], o)):
        return None
    samples = []
    for bp in _bootstrap_points(segs, groups, rng):
        if any(p is None for p in bp):
            continue
        ob = _orthocentre(*bp)
        if ob is not None:
            samples.append(ob)
    if len(samples) < BOOTSTRAP // 2:
        return None
    cov = np.cov(np.array(samples).T) + np.eye(2) * PP_SYSTEMATIC ** 2
    d2 = float(o @ np.linalg.solve(cov, o))
    return o, d2


def _boot(segs, g, rng):
    """Bootstrapped vanishing points of a group (cached on it)."""
    if getattr(g, 'boot', None) is None:
        out = []
        for _ in range(BOOTSTRAP):
            out.append(_fit(segs, rng.choice(g.idx, len(g.idx)), g.v, rounds=2))
        g.boot = np.array(out)
    return g.boot


def _square_angle(v_vert, va, vb):
    """Degrees by which the horizon through va, vb is not square to the
    direction of parallel verticals (v_vert at infinity)."""
    hl = np.cross(va, vb)
    hdir = np.array([hl[1], -hl[0]])
    vdir = v_vert[:2]
    n1, n2 = np.linalg.norm(hdir), np.linalg.norm(vdir)
    if n1 < 1e-15 or n2 < 1e-15:
        return 90.0
    return math.degrees(math.asin(min(1.0, abs(float(hdir @ vdir)) / (n1 * n2))))


def horizon_square(segs, V, a, b, rng):
    """(angle, ok): with parallel verticals (a level camera) the horizon of
    two horizontal groups must be square to them within the error (3
    bootstrap spreads, at least 0.2 degrees): lines that are not horizontal
    (a slanted roof, stairs) fail. Converging verticals: the principal point
    is not known yet, nothing to check here (3 Points / 2 Points do)."""
    if V is None or not V.parallel:
        return 0.0, True
    vinf = np.array([V.v[0], V.v[1], 0.0])
    ang = _square_angle(vinf, a.v, b.v)
    bv, ba, bb = _boot(segs, V, rng), _boot(segs, a, rng), _boot(segs, b, rng)
    spread = float(np.std([_square_angle(np.array([x[0], x[1], 0.0]), y, z)
                           for x, y, z in zip(bv, ba, bb)]))
    return ang, ang <= max(3.0 * spread, 0.2)


# --------------------------------------------------------------------------
# Choosing the mode
# --------------------------------------------------------------------------

class Detection:
    def __init__(self, ok, reason, mode=None, roles=None, segs=None, groups=(), info=None):
        self.ok, self.reason, self.mode = ok, reason, mode
        self.roles = roles or {}            # 'V' / 'H1' / 'H2' -> Group
        self.segs = segs
        self.groups = list(groups)
        self.info = info or {}

    def lines(self, role):
        """Two segments of a role (relative coordinates) for the stored lines:
        of the longest, the pair with the largest length x length x distance
        between them - their crossing (the vanishing point) is sharp and a hand
        edit of one line moves it little. plan() then turns them to pass
        through the vanishing point of ALL the role's segments."""
        g = self.roles.get(role)
        if g is None:
            return []
        s = self.segs
        idx = g.idx[np.argsort(-s.L[g.idx])][:24]
        if len(idx) < 3:
            return s.rel(idx)
        mid = np.c_[s.M[idx], np.ones(len(idx))]
        dist = np.abs(s.lines[idx] @ mid.T)                 # line i <-> middle of j
        score = s.L[idx][:, None] * s.L[idx][None, :] * np.minimum(dist, dist.T)
        i, j = np.unravel_index(int(np.argmax(score)), score.shape)
        return s.rel([idx[min(i, j)], idx[max(i, j)]])

    def point(self, role):
        """Homogeneous vanishing point of ALL the role's segments (at infinity
        for a parallel group)."""
        g = self.roles[role]
        return _fit(self.segs, g.idx, infinite=True) if g.parallel else g.v


def choose(segs, groups, rng):
    """The mode from the groups (see the module docstring and AGENTS.md)."""
    if not groups:
        return Detection(False, 'too few straight edges on the photo', segs=segs)
    # the verticals: the strongest group that runs up the photo (within 45
    # degrees: a camera turned up and rolled); horizontals: clearly sideways
    verts = [g for g in groups if g.vertical]
    V = max(verts, key=lambda g: g.support) if verts else None
    hors = [g for g in groups if g is not V and g.tilt >= 30.0]
    centre = np.zeros(2)
    # two horizontal directions: the strongest pair whose horizon is square to
    # the verticals within the error (a level horizon for parallel verticals)
    # and that can be perpendicular (opposite sides of the centre)
    H1, H2 = (hors[0] if hors else None), None
    tested = []
    for a, b in sorted(((a, b) for i, a in enumerate(hors) for b in hors[i + 1:]),
                       key=lambda ab: -(ab[0].support + ab[1].support)):
        pa, pb = a.point, b.point
        if pa is not None and pb is not None:
            # level camera: principal point on the horizon (Shift X 0), so only x counts
            f2 = -float(pa[0] * pb[0]) if V is not None and V.parallel else _focal2(pa, pb, centre)
            if not _plausible_f(f2):
                continue
        square = horizon_square(segs, V, a, b, rng)
        tested.append(round(square[0], 2))
        if square[1]:
            H1, H2 = a, b
            break
    roles = {k: g for k, g in (('V', V), ('H1', H1), ('H2', H2)) if g is not None}
    for g in groups:
        g.role = next((k for k, x in roles.items() if x is g), None)
    finite = [k for k, g in roles.items() if not g.parallel]
    parallel = [k for k, g in roles.items() if g.parallel]
    info = dict(finite=finite, parallel=parallel, horizon_tested=tested,
                stats={k: round(g.parallel_stat, 1) for k, g in roles.items()})
    det = lambda ok, why, mode=None: Detection(ok, why, mode, roles, segs, groups, info)

    # 3 Points: three strong vanishing points and a principal point that is
    # reliably not the centre (otherwise 2 Points is steadier)
    if len(finite) == 3:
        res = principal_point_test(segs, [V, H1, H2], rng)
        if res is not None:
            info['pp'], info['pp_d2'] = res[0].tolist(), round(res[1], 1)
            if res[1] > ORTHO_D2:
                return det(True, 'three vanishing points, principal point off centre', 'THIRD_VP')
    # 1 Point: one vanishing point, the other two directions parallel on the photo
    if len(finite) == 1 and len(parallel) == 2:
        return det(True, 'one vanishing point, the other lines are parallel', 'ONE_VP')
    # Parallel Verticals: verticals parallel within their error, two horizontals
    if V is not None and V.parallel and H1 is not None and H2 is not None and \
            not (H1.parallel and H2.parallel):
        spread = pair_spread(segs, H1, H2, rng, level=True) \
            if not (H1.parallel or H2.parallel) else 0.0
        info['spread'] = {'H1H2': round(spread, 4)}
        if spread <= MAX_F_SPREAD:
            return det(True, 'verticals are parallel', 'VERTICALS')
    # 2 Points: of the finite points that can be perpendicular, the pair whose
    # focal length is the least uncertain
    best = None
    for i, ka in enumerate(finite):
        for kb in finite[i + 1:]:
            a, b = roles[ka], roles[kb]
            if _plausible_f(_focal2(a.point, b.point, centre)):
                spread = pair_spread(segs, a, b, rng)
                info.setdefault('spread', {})[ka + kb] = round(spread, 4)
                if best is None or spread < best[0]:
                    best = (spread, ka, kb)
    if best is not None and best[0] <= MAX_F_SPREAD:
        pair = sorted(best[1:])
        why = 'two vanishing points' + (' (one horizontal and the verticals)'
                                        if 'V' in pair else '')
        d = det(True, why, 'TWO_VP')
        d.info['pair'] = pair
        return d
    if len(hors) >= 2 and H2 is None:
        return det(False, 'the horizontal lines do not share one level horizon '
                          '(slanted or curved shapes?)')
    spreads = [v for v in info.get('spread', {}).values() if math.isfinite(v)]
    if spreads:
        return det(False, f'the focal length is not reliable (varies {100.0 * min(spreads):.0f} % '
                          'with the lines taken): too few lines of a direction, or its '
                          'vanishing point too far away')
    if len(finite) == 1:
        return det(False, 'only one direction converges: not enough for a camera')
    if not finite:
        return det(False, 'the lines do not converge (no perspective found)')
    return det(False, 'the directions found cannot be at right angles to each other '
                      '(lines of other shapes?)')


def detect(pixels, seed=0):
    """Detection for a photo (rows from the top)."""
    rng = np.random.default_rng(seed)
    gray = downscale(to_gray(pixels))
    h, w = gray.shape
    p1, p2, _wd = segments(gray)
    if len(p1) < 4:
        return Detection(False, 'too few straight edges on the photo')
    segs = Segs(p1, p2, w, h)
    groups = manhattan_groups(segs, rng)
    return choose(segs, groups, rng)


# --------------------------------------------------------------------------
# Detection -> lines and axes of the add-on
# --------------------------------------------------------------------------

def _rodrigues(w):
    t = math.sqrt(float(w @ w))
    if t < 1e-15:
        return np.eye(3)
    k = w / t
    K = np.array([[0.0, -k[2], k[1]], [k[2], 0.0, -k[0]], [-k[1], k[0], 0.0]])
    return np.eye(3) + math.sin(t) * K + (1.0 - math.cos(t)) * (K @ K)


AXIS_INDEX = {'X': 0, 'Y': 1, 'Z': 2}


def refine(segs, sets, st, mode):
    """The camera fitted to ALL segments of the three directions at once
    (Gauss-Newton on their angles, weighted by length), with the directions
    exactly square (a rotation) - instead of each vanishing point by itself
    and the camera from a few lines. sets: {axis: segment indices}. 2 Points:
    rotation + focal length, principal point at the centre; 3 Points: also
    the principal point; Parallel Verticals: heading + focal length + horizon
    height, level. Returns a model.CamState (st if it does not get better)."""
    R0 = np.asarray(st.R, float)
    pp0 = np.asarray(st.pp, float)
    axes = [(AXIS_INDEX[a], np.asarray(idx)) for a, idx in sets.items() if len(idx)]
    idx_all = np.concatenate([i for _k, i in axes])
    if mode == 'VERTICALS':
        n_par = 3                                   # heading, log f, horizon height

        def unpack(x):
            c, s = math.cos(x[0]), math.sin(x[0])
            rz = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
            return R0 @ rz, st.f * math.exp(x[1]), np.array([pp0[0], pp0[1] + x[2]])
    else:
        n_par = 6 if mode == 'THIRD_VP' else 4      # rotation (3), log f, [principal point]

        def unpack(x):
            pp = pp0 + (x[4:6] if mode == 'THIRD_VP' else 0.0)
            return _rodrigues(x[:3]) @ R0, st.f * math.exp(x[3]), pp

    def residuals(x):
        R, f, pp = unpack(x)
        out = []
        for k, idx in axes:
            d = R[:, k]                             # world axis in camera space
            v = np.array([f * d[0] - pp[0] * d[2], f * d[1] - pp[1] * d[2], -d[2]])
            e = v[None, :2] - segs.M[idx] * v[2]
            D = segs.D[idx]
            e *= np.where(np.sum(D * e, axis=1) < 0.0, -1.0, 1.0)[:, None]
            ne = np.maximum(np.hypot(e[:, 0], e[:, 1]), 1e-300)
            out.append(0.5 * segs.L[idx] * (D[:, 0] * e[:, 1] - D[:, 1] * e[:, 0]) / ne)
        return np.concatenate(out)

    x = np.zeros(n_par)
    r = residuals(x)
    cost0 = cost = float(r @ r)
    lam = 1e-3
    for _ in range(20):
        J = np.empty((len(r), n_par))
        for j in range(n_par):
            dx = np.zeros(n_par)
            dx[j] = 1e-6
            J[:, j] = (residuals(x + dx) - r) / 1e-6
        A, g = J.T @ J, J.T @ r
        step = -np.linalg.solve(A + lam * np.diag(np.diag(A) + 1e-12), g)
        r_new = residuals(x + step)
        c_new = float(r_new @ r_new)
        if c_new < cost:
            x, r, cost, lam = x + step, r_new, c_new, lam * 0.3
            if np.max(np.abs(step)) < 1e-9:
                break
        else:
            lam *= 10.0
            if lam > 1e8:
                break
    if not cost < cost0 or len(idx_all) < 4:
        return st
    R, f, pp = unpack(x)
    if not _plausible_f(f * f):
        return st
    C = tuple(st.C)
    return M.CamState(aspect=st.aspect, f=float(f), pp=(float(pp[0]), float(pp[1])),
                      R=tuple(tuple(float(c) for c in row) for row in R), C=C)


def plan(det, current):
    """(mode, axis 1, axis 2, lines, state) for the add-on, or None: the
    vertical group is Z; the horizontal ones are X / Y, whichever way gives
    the camera closest to `current` (model.CamState), as orient_axes does for
    the signs. lines: {axis: [(a, b), ...]} of every group found (relative)."""
    if not det.ok:
        return None
    roles = det.roles
    hs = [k for k in ('H1', 'H2') if k in roles]
    maps = [dict(zip(hs, xy)) for xy in (('X', 'Y'), ('Y', 'X'))][:2 if len(hs) == 2 else 2]
    if len(hs) == 1:
        maps = [{hs[0]: 'X'}, {hs[0]: 'Y'}]
    best = None
    for m in maps:
        m = dict(m, V='Z')
        # two lines per direction, turned (about their middles) to pass through
        # the vanishing point of all its segments: they give that point exactly
        lines = {m[k]: [M.retarget_segment(seg, tuple(det.point(k)), current.aspect)
                        for seg in det.lines(k)]
                 for k in roles if k in m}
        if det.mode == 'TWO_VP':
            a1, a2 = (m[k] for k in det.info['pair'])
        elif det.mode == 'ONE_VP':
            a1 = m[det.info['finite'][0]]
            a2 = 'Y' if a1 == 'X' else 'X'
        else:
            a1, a2 = 'X', 'Y'
        res = M.solve_lines(lines, None, det.mode, a1, a2, current, reset_shift=True)
        if not res.ok:
            continue
        d = M._rot_dist(res.state.R, current.R)
        if best is None or d < best[0]:
            best = (d, (det.mode, a1, a2, lines, res.state, m))
    if best is None:
        return None
    mode, a1, a2, lines, st, m = best[1]
    if mode != 'ONE_VP':
        # all segments of the three directions together, then the written lines
        # turned (about their middles, a fraction of a pixel) to pass exactly
        # through the refined vanishing points: solving them gives that camera
        sets = {m[k]: g.idx for k, g in det.roles.items() if k in m}
        st2 = refine(det.segs, sets, st, mode)
        if st2 is not st:
            aimed = {axis: [M.retarget_segment(seg, st2.vp_h(M.AXIS_VECTORS[axis]), st2.aspect)
                            for seg in segs]
                     for axis, segs in lines.items()}
            res = M.solve_lines(aimed, None, mode, a1, a2, st2, reset_shift=mode != 'THIRD_VP')
            if res.ok:
                lines, st = aimed, res.state
    return mode, a1, a2, lines, st
