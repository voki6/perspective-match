# SPDX-License-Identifier: GPL-3.0-or-later
"""Drawing in the camera view of the 3D viewport.

Cheap by design:
* returns at once when no camera is picked anywhere (state.active),
* draws only in a camera view that looks through the picked camera,
* geometry and GPU batches are cached; they are rebuilt only when the lines,
  the camera, the view (frame rectangle) or the edit state change.

Theme axis / selection colours, the UI font and size, anti-aliased lines and
round points (draw2d.py), text with an outline (readable on light and dark
photos); follows the viewport's Overlays switch.
"""

import math

import blf
import bpy
import gpu
from bpy_extras.view3d_utils import location_3d_to_region_2d

from . import camera
from . import draw2d
from . import handles as H
from . import model as M
from . import solver as S
from . import state

_handle = None
_cache = {'key': None, 'items': None}

HANDLE_RADIUS = 5.0          # points (x UI scale), as in the earlier versions
RIM = 1.5


# --------------------------------------------------------------------------
# Camera frame <-> region pixels
# --------------------------------------------------------------------------

def compute_frame_rect(scene, region, rv3d, cam_obj):
    """(left, bottom, right, top) of the camera frame in region pixels, or None."""
    frame = cam_obj.data.view_frame(scene=scene)
    mw = cam_obj.matrix_world
    pts = [location_3d_to_region_2d(region, rv3d, mw @ v) for v in frame]
    if any(p is None for p in pts):
        return None
    xs = [p.x for p in pts]
    ys = [p.y for p in pts]
    rect = (min(xs), min(ys), max(xs), max(ys))
    if rect[2] - rect[0] < 1.0 or rect[3] - rect[1] < 1.0:
        return None
    return rect


def rel_to_px(rect, p):
    left, bottom, right, top = rect
    return (left + p[0] * (right - left), top - p[1] * (top - bottom))


def px_to_rel(rect, p):
    left, bottom, right, top = rect
    return ((p[0] - left) / (right - left), (top - p[1]) / (top - bottom))


# --------------------------------------------------------------------------
# Theme
# --------------------------------------------------------------------------

def _theme():
    return bpy.context.preferences.themes[0]


def axis_color(axis_letter, alpha=1.0):
    ui = _theme().user_interface
    c = {'X': ui.axis_x, 'Y': ui.axis_y, 'Z': ui.axis_z}.get(axis_letter.strip('+-'))
    if c is None:
        return (0.8, 0.8, 0.8, alpha)
    return (c[0], c[1], c[2], alpha)


HORIZON_COLOR = (1.0, 0.85, 0.35, 1.0)
PP_COLOR = (1.0, 1.0, 1.0, 0.95)
PP_MOVED_COLOR = (1.0, 0.35, 0.85, 1.0)     # moved away from the mode's default (pink:
                                            # rare in photos, light enough on dark ones)
PP_CROSS = 11.0                             # half length of the principal point cross (pt)
SCALE_TICK = 6.0                            # half length of the ticks of the Scale line (pt)
SCALE_DASH = (6.0, 4.0)                     # dash and gap of the Scale line (pt)
SCALE_COLOR = (1.0, 1.0, 1.0, 1.0)          # the Scale line and its end point
SCALE_OUTLINE = (0.0, 0.0, 0.0, 0.6)
ORIGIN_COLOR = (1.0, 0.9, 0.2, 1.0)
ERROR_COLOR = (1.0, 0.3, 0.25, 1.0)
WARNING_COLOR = (1.0, 0.75, 0.2, 1.0)
NOTICE_COLOR = (1.0, 1.0, 1.0, 1.0)
OUTLINE = (0.0, 0.0, 0.0, 0.9)
WHITE = (1.0, 1.0, 1.0, 1.0)
CLEAR = (1.0, 1.0, 1.0, 0.0)


def group_color(group, alpha=1.0):
    if group in ('X', 'Y', 'Z'):
        return axis_color(group, alpha)
    if group == 'origin':
        return ORIGIN_COLOR[:3] + (alpha,)
    return (0.8, 0.8, 0.8, alpha)


def selected_color():
    c = _theme().view_3d.vertex_select
    return (c[0], c[1], c[2], 1.0)


def _sizes():
    prefs = bpy.context.preferences
    ui = prefs.system.ui_scale or 1.0
    px = prefs.system.pixel_size or 1.0
    try:
        font = prefs.ui_styles[0].widget.points
    except (AttributeError, IndexError):
        font = 11.0
    return dict(ui=ui, line=px, font=font * ui)


# --------------------------------------------------------------------------
# Building the cached draw list
# --------------------------------------------------------------------------

def _loop(pts):
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]


def _clip_extension(start, end, limit):
    dx, dy = end[0] - start[0], end[1] - start[1]
    n = math.hypot(dx, dy)
    if n <= limit or n == 0.0:
        return end
    return (start[0] + dx / n * limit, start[1] + dy / n * limit)


def clip_line_to_unit_square(p, d):
    """Part of the infinite line p + t d inside [0, 1]^2 (relative frame)."""
    t0, t1 = -math.inf, math.inf
    for i in range(2):
        if abs(d[i]) < 1e-12:
            if p[i] < 0.0 or p[i] > 1.0:
                return None
            continue
        a = (0.0 - p[i]) / d[i]
        b = (1.0 - p[i]) / d[i]
        t0 = max(t0, min(a, b))
        t1 = min(t1, max(a, b))
    if t0 > t1:
        return None
    return ((p[0] + t0 * d[0], p[1] + t0 * d[1]), (p[0] + t1 * d[0], p[1] + t1 * d[1]))


def group_vp_rel(segs, aspect):
    """Vanishing point (relative) of a list of relative segments, or None."""
    if len(segs) < 2:
        return None
    vp, _err = S.vanishing_point([(S.rel_to_plane(a, aspect), S.rel_to_plane(b, aspect))
                                  for a, b in segs])
    return None if vp is None else S.plane_to_rel(vp, aspect)


LINES_HIDDEN_MESSAGE = 'Lines are hidden: turn on Lines in Perspective > Display to edit them'


def _scale_ruler(segs_out, texts, o, end, label, sz):
    """The known length (Scale), drawn like Blender's Measure tool: a light
    dashed line with a dark outline (readable on any photo), ticks across both
    ends and the length beside its middle."""
    ui, lw = sz['ui'], sz['line']
    dx, dy = end[0] - o[0], end[1] - o[1]
    n = math.hypot(dx, dy)
    if n < 1e-6:
        return
    ux, uy = dx / n, dy / n
    tx, ty = -uy * SCALE_TICK * ui, ux * SCALE_TICK * ui
    pieces = [((p[0] - tx, p[1] - ty), (p[0] + tx, p[1] + ty)) for p in (o, end)]
    dash, gap = SCALE_DASH[0] * ui, SCALE_DASH[1] * ui
    t = 0.0
    while t < n:
        t1 = min(t + dash, n)
        pieces.append(((o[0] + ux * t, o[1] + uy * t), (o[0] + ux * t1, o[1] + uy * t1)))
        t = t1 + gap
    segs_out.extend((a, b, 3.5 * lw, SCALE_OUTLINE) for a, b in pieces)
    segs_out.extend((a, b, 1.5 * lw, SCALE_COLOR) for a, b in pieces)
    # the text's box beside the middle of the line, on its upper side
    blf.size(0, sz['font'])
    w, h = blf.dimensions(0, label)
    nx, ny = (-uy, ux) if ux >= 0.0 else (uy, -ux)
    if ny < 0.0:
        nx, ny = -nx, -ny
    off = 0.5 * abs(nx) * w + 0.5 * abs(ny) * h + 6.0 * ui
    mx, my = o[0] + 0.5 * dx + nx * off, o[1] + 0.5 * dy + ny * off
    texts.append(((mx - 0.5 * w, my - 0.5 * h), label, SCALE_COLOR, 0.0))


def build(context, cam_obj, rect, editing):
    """(line segments, discs, texts, top line segments) to draw."""
    region = context.region
    scene = context.scene
    tool = camera.tool_of(scene)
    pm = camera.settings_of(cam_obj.data)
    session = state.session
    sz = _sizes()
    ui, lw = sz['ui'], sz['line']
    show_lines = tool.show_overlay          # also while editing: hidden lines hide their points
    cam_state = camera.current_state(cam_obj, scene)           # the truth
    aspect = cam_state.aspect
    lines = camera.lines_dict(pm)
    to_px = lambda p: rel_to_px(rect, p)  # noqa: E731
    diag = math.hypot(region.width, region.height)
    segs_out, discs, texts, top = [], [], [], []

    if show_lines:
        for group in H.visible_line_groups(tool):
            color = group_color(group)
            segs = lines.get(group, [])
            vp_rel = group_vp_rel(segs, aspect)
            vp_px = to_px(vp_rel) if vp_rel is not None else None
            for a_rel, b_rel in segs:
                a, b = to_px(a_rel), to_px(b_rel)
                if vp_px is not None:
                    da = math.hypot(vp_px[0] - a[0], vp_px[1] - a[1])
                    db = math.hypot(vp_px[0] - b[0], vp_px[1] - b[1])
                    near = a if da < db else b
                    segs_out.append((near, _clip_extension(near, vp_px, 3.0 * diag), 1.0 * lw,
                                     color[:3] + (0.45,)))
            for a_rel, b_rel in segs:
                segs_out.append((to_px(a_rel), to_px(b_rel), (2.5 if editing else 2.0) * lw, color))
            if vp_px is not None and 0 <= vp_px[0] <= region.width and 0 <= vp_px[1] <= region.height:
                discs.append((vp_px, 3.0 * ui, 0.0, color, color))
                texts.append(((vp_px[0] + 6 * ui, vp_px[1] + 6 * ui), group, color, 0.0))

    if tool.show_horizon:
        h = cam_state.horizon_rel()
        clipped = None
        if h is not None:
            clipped = clip_line_to_unit_square(h[0], (h[1][0] - h[0][0], h[1][1] - h[0][1]))
        if clipped:
            p0, p1 = to_px(clipped[0]), to_px(clipped[1])
            if p1[0] < p0[0]:
                p0, p1 = p1, p0
            segs_out.append((p0, p1, 1.5 * lw, HORIZON_COLOR))
            ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
            nx, ny = -math.sin(ang), math.cos(ang)
            off = 5.0 * ui
            texts.append(((p0[0] + 10 * ui * math.cos(ang) + off * nx,
                           p0[1] + 10 * ui * math.sin(ang) + off * ny), 'Horizon', HORIZON_COLOR, ang))

    origin_visible = False
    pp_color = PP_COLOR
    if show_lines:
        o = cam_state.project_rel((0.0, 0.0, 0.0))
        origin_visible = o is not None
        if o is not None:
            dist = math.sqrt(sum(c * c for c in cam_state.C))
            for letter, vec in (('X', (1, 0, 0)), ('Y', (0, 1, 0)), ('Z', (0, 0, 1))):
                tip = cam_state.project_rel(tuple(camera.ORIGIN_AXIS_LENGTH * dist * c
                                                  for c in vec))
                if tip is not None:
                    segs_out.append((to_px(o), to_px(tip), 2.0 * lw, axis_color(letter)))
            # the known length is a measuring line of its own over the axes, so
            # the axes always show their true directions
            if pm.use_scale and M.scale_axis_usable(cam_state, pm.scale_axis):
                _scale_ruler(segs_out, texts, to_px(o), to_px(tuple(pm.scale_point)),
                             camera.length_text(scene, pm.scale_length), sz)
        pp_plane = camera.principal_point(cam_obj, cam_state)
        pp = to_px(S.plane_to_rel(pp_plane, aspect))
        s = PP_CROSS * ui
        pp_color = PP_MOVED_COLOR if M.principal_point_moved(tool.mode, pp_plane) else PP_COLOR
        # under its handle point (the discs are drawn after the lines)
        segs_out.append(((pp[0] - s, pp[1]), (pp[0] + s, pp[1]), 1.5 * lw, pp_color))
        segs_out.append(((pp[0], pp[1] - s), (pp[0], pp[1] + s), 1.5 * lw, pp_color))

    # Handles: round points with a dark rim (white rim when selected)
    r = HANDLE_RADIUS * ui
    if editing:
        hover = session.get('hover')
        sel = selected_color()
        for key in H.iter_handle_keys(pm, tool, origin_visible):   # none while Lines is off
            co = H.get_co(pm, key)
            if co is None:
                continue
            selected = H.is_selected(key)
            color = SCALE_COLOR if key[0] == 'scale' else group_color(key[0])
            discs.append((to_px(co), r * 1.35 if key == hover else r, RIM * ui,
                          sel if selected else color, WHITE if selected else OUTLINE))
        pp_rel = camera.principal_point_rel(cam_obj, scene)
        if pp_rel is not None:                 # the principal point can be dragged
            key = ('pp', 0, None)
            selected = H.is_selected(key)
            # same size as the other points, drawn over its cross
            discs.append((to_px(pp_rel), r * 1.35 if key == hover else r, RIM * ui,
                          sel if selected else pp_color, WHITE if selected else OUTLINE))
        snap_to = session.get('snap_target')
        if snap_to is not None:
            discs.append((snap_to, 2.2 * r, 2.0 * lw, CLEAR, WHITE))
        if session.get('box'):
            x0, y0, x1, y1 = session['box']
            for a, b in _loop([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]):
                top.append((a, b, 1.0 * lw, (1.0, 1.0, 1.0, 0.9)))
    elif show_lines and origin_visible:
        discs.append((to_px(tuple(pm.origin)), 0.8 * r, RIM * ui, ORIGIN_COLOR, OUTLINE))

    # Messages, bottom left inside the frame (the mode and the running transform
    # are shown in the viewport header, like Blender's own tools)
    left, bottom, right, top_y = rect
    line_h = sz['font'] * 1.6
    msgs = []
    if editing and not show_lines:
        msgs.append((LINES_HIDDEN_MESSAGE, WARNING_COLOR))
    errors = state.errors.get(camera.key(cam_obj)) if editing else None
    if errors:
        for a, b in _loop([(left, bottom), (right, bottom), (right, top_y), (left, top_y)]):
            top.append((a, b, 2.0 * lw, ERROR_COLOR))
        msgs += [(m, ERROR_COLOR) for m in errors]
    warnings = list(camera.aspect_info(cam_obj.data, scene)[1])
    if editing:
        warnings += state.warnings.get(camera.key(cam_obj), [])
    msgs += [(m, WARNING_COLOR) for m in warnings]
    note = camera.notice()
    if note:
        msgs.append((note, NOTICE_COLOR))
    y = bottom + 0.6 * line_h
    for msg, color in reversed(msgs):
        texts.append(((left + 8 * ui, y), msg, color, 0.0))
        y += line_h
    return (draw2d.lines_batch(segs_out), draw2d.discs_batch(discs), texts,
            draw2d.lines_batch(top))


# --------------------------------------------------------------------------
# Drawing
# --------------------------------------------------------------------------

def _draw_text(pos, text, color, angle, size):
    font = 0
    blf.size(font, size)
    blf.color(font, *color)
    blf.enable(font, blf.SHADOW)
    try:
        blf.shadow(font, 6, 0.0, 0.0, 0.0, 0.9)          # 6 = outline
    except (ValueError, TypeError):
        blf.shadow(font, 5, 0.0, 0.0, 0.0, 0.9)
    blf.shadow_offset(font, 0, 0)
    if angle:
        blf.enable(font, blf.ROTATION)
        blf.rotation(font, angle)
    blf.position(font, pos[0], pos[1], 0)
    blf.draw(font, text)
    if angle:
        blf.disable(font, blf.ROTATION)
    blf.disable(font, blf.SHADOW)


def _draw_items(items):
    lines, discs, texts, top = items
    gpu.state.blend_set('ALPHA')
    draw2d.draw(lines, 'lines')
    draw2d.draw(discs, 'discs')
    draw2d.draw(top, 'lines')
    size = _sizes()['font']
    for pos, text, color, angle in texts:
        _draw_text(pos, text, color, angle, size)
    gpu.state.blend_set('NONE')


def store_rect(region, rect):
    """Remember the camera frame of a region (the edit mode picks with it). A
    region seen for the first time is the moment to drop regions that are gone."""
    ptr = region.as_pointer()
    if ptr not in state.frame_rects:
        live = set()
        wm = bpy.context.window_manager
        for window in (wm.windows if wm else ()):
            for area in (window.screen.areas if window.screen else ()):
                if area.type == 'VIEW_3D':
                    live.update(r.as_pointer() for r in area.regions if r.type == 'WINDOW')
        for old in [k for k in state.frame_rects if k not in live]:
            del state.frame_rects[old]
    state.frame_rects[ptr] = rect


def draw_callback():
    if not state.active:                 # nothing picked anywhere: no work at all
        return
    context = bpy.context
    rv3d = context.region_data
    if rv3d is None or rv3d.view_perspective != 'CAMERA':
        return
    scene = context.scene
    cam_obj = camera.active_camera(scene)
    if cam_obj is None:
        return
    space = context.space_data
    viewed = space.camera if (space.use_local_camera and space.camera) else scene.camera
    if viewed != cam_obj or not camera.is_started(cam_obj):
        return
    region = context.region
    rect = compute_frame_rect(scene, region, rv3d, cam_obj)
    if rect is None:
        return
    store_rect(region, rect)
    if not space.overlay.show_overlays:
        return
    tool = camera.tool_of(scene)
    session = state.session
    editing = (session is not None and session.get('camera') == cam_obj.name
               and session.get('region') == region.as_pointer())
    if not (tool.show_overlay or tool.show_horizon or editing):
        return
    key = (state.version, region.as_pointer(), tuple(round(v, 2) for v in rect),
           tool.show_overlay, tool.show_horizon, tool.mode, tool.vp1_axis, tool.vp2_axis,
           editing, frozenset(state.selection) if editing else None,
           tuple(sorted((k, str(v)) for k, v in session.items())) if editing else None)
    if _cache['key'] != key:
        _cache['items'] = build(context, cam_obj, rect, editing)
        _cache['key'] = key
    _draw_items(_cache['items'])


def _safe_draw():
    try:
        draw_callback()
        state.last_draw_error = None
    except ReferenceError:
        _cache['key'] = None
    except Exception as ex:          # keep Blender usable, but remember why
        _cache['key'] = None
        if state.last_draw_error != repr(ex):
            import traceback
            traceback.print_exc()
        state.last_draw_error = repr(ex)
        gpu.state.blend_set('NONE')


def register():
    global _handle
    if _handle is None:
        _handle = bpy.types.SpaceView3D.draw_handler_add(_safe_draw, (), 'WINDOW', 'POST_PIXEL')
    camera.tag_redraw_view3d()


def unregister():
    global _handle
    if _handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_handle, 'WINDOW')
        _handle = None
    draw2d.clear()
    _cache['key'] = None
    _cache['items'] = None
    state.frame_rects.clear()
    camera.tag_redraw_view3d()
