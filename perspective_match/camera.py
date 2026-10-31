# SPDX-License-Identifier: GPL-3.0-or-later
"""Bridge between Blender and model.py.

camera -> lines   check_scene(): from a depsgraph_update_post handler, whenever
                  the matched camera (or its lens, shift, sensor, the render size
                  or the photo) changed since the lines were last matched to it.
lines -> camera   solve_and_apply(): while the lines are edited.

Loop guard: after either direction the camera "signature" is remembered (runtime
only, state.signatures). The handler re-aims the lines only when the camera no
longer matches it, so a camera written by the add-on never moves the lines.
After loading a file, undo or redo the stored lines and camera belong together:
the signature is dropped, and the first depsgraph update takes it again without
re-aiming (only then is the camera's matrix_world evaluated).

Performance: every handler returns at once while no camera is picked
(state.active), and the overlay is rebuilt only when state.version changes.
Redraws asked for while an event is handled are merged into one (one_redraw).
"""

import math
import time
from contextlib import contextmanager

import bpy
import mathutils

from . import model as M
from . import props as P
from . import solver as S
from . import state

ASPECT_TOLERANCE = 1e-3
ASPECT_WARNING = 'Render proportions differ from the photo'
LENS_RANGE = (1.0, 5000.0)       # what Blender accepts
ORIGIN_AXIS_LENGTH = 0.12        # axes drawn at the origin: share of the camera distance
NOTICE_SECONDS = 6.0             # how long a notice stays in the camera view


def settings_of(cam_data):
    return getattr(cam_data, 'perspective_match', None)


def tool_of(scene):
    return getattr(scene, 'perspective_match', None)


def scene_camera(scene):
    cam = scene.camera if scene else None
    return cam if cam is not None and cam.type == 'CAMERA' else None


def active_camera(scene, started=False):
    """THE camera of the add-on: the one picked in the panel for this scene, or
    None. started=True: only once it has its lines (it always has them once
    picked; False only for a moment while it is being picked)."""
    tool = tool_of(scene) if scene else None
    cam = tool.camera if tool else None
    if cam is None or cam.type != 'CAMERA':
        return None
    if started and not is_started(cam):
        return None
    return cam


def key(cam_obj):
    """Key of a camera in the runtime state (signatures, errors, warnings)."""
    return cam_obj.name


def is_started(cam_obj):
    pm = settings_of(cam_obj.data) if cam_obj is not None else None
    return bool(pm and pm.started)


def update_active():
    state.active = any(active_camera(s) is not None for s in bpy.data.scenes)


def changed():
    """Lines, camera or display settings changed: rebuild the overlay."""
    state.version += 1
    tag_redraw_view3d()


def notify(text):
    """A short notice over the photo in the camera view, with the other
    messages (e.g. the render size was changed), gone after a few seconds.
    Used where an operator report is not possible (property updates must not
    call operators); not in the panel, so the panel does not jump."""
    stamp = time.monotonic()
    state.notice = (text, stamp)
    changed()

    def clear():
        if state.notice is not None and state.notice[1] == stamp:
            state.notice = None
            changed()
        return None

    bpy.app.timers.register(clear, first_interval=NOTICE_SECONDS)


def notice():
    return state.notice[0] if state.notice else None


# --------------------------------------------------------------------------
# Photo and aspect ratio
# --------------------------------------------------------------------------

def image_size(image):
    if image is None:
        return (0, 0)
    try:
        w, h = image.size
    except (ReferenceError, AttributeError):
        return (0, 0)
    return (w, h)


def photo(cam_data):
    """The photo is the camera's (first usable) background image."""
    for bg in cam_data.background_images:
        if bg.source == 'IMAGE' and bg.image is not None:
            w, h = image_size(bg.image)
            if w > 0 and h > 0:
                return bg.image
    return None


def render_aspect(scene):
    r = scene.render
    h = r.resolution_y * r.pixel_aspect_y
    return (r.resolution_x * r.pixel_aspect_x) / h if h > 0 else 0.0


def fit_render_to_image(scene, image):
    """Resolution = photo size in pixels, square pixels. Resolution % is left alone:
    it is the way to render smaller or bigger with the same proportions."""
    w, h = image_size(image)
    if w <= 0 or h <= 0:
        return False
    r = scene.render
    if (r.resolution_x, r.resolution_y) != (w, h):
        r.resolution_x, r.resolution_y = w, h
    if r.pixel_aspect_x != 1.0 or r.pixel_aspect_y != 1.0:
        r.pixel_aspect_x = r.pixel_aspect_y = 1.0
    return True


def apply_photo_resolution(scene, cam_obj):
    """Render resolution from the camera's photo. The resolution belongs to the
    scene, so this only happens on an explicit action, and says so (notify).
    Returns the message, or None when nothing changed."""
    img = photo(cam_obj.data)
    if img is None:
        return None
    w, h = image_size(img)
    r = scene.render
    if w <= 0 or (r.resolution_x, r.resolution_y, r.pixel_aspect_x, r.pixel_aspect_y) == (w, h, 1.0, 1.0):
        return None
    fit_render_to_image(scene, img)
    text = f'Render resolution set to {w} \u00d7 {h} (photo of "{cam_obj.name}")'
    notify(text)
    return text


def aspect_info(cam_data, scene):
    """(aspect of the frame the lines live in, warnings). The photo's aspect
    if there is one, otherwise the render's."""
    ra = render_aspect(scene)
    w, h = image_size(photo(cam_data))
    if w > 0 and h > 0:
        ia = w / h
        if ra > 0 and abs(ra / ia - 1.0) > ASPECT_TOLERANCE:
            return ia, [ASPECT_WARNING]
        return ia, []
    return ra, []


# --------------------------------------------------------------------------
# Camera state <-> Blender
# --------------------------------------------------------------------------

def current_state(cam_obj, scene):
    cd = cam_obj.data
    aspect, _w = aspect_info(cd, scene)
    if aspect <= 0:
        aspect = 1.0
    mw = cam_obj.matrix_world
    rows = [list(mw[i]) for i in range(4)]
    return M.state_from_blender(rows, cd.lens, cd.shift_x, cd.shift_y, cd.sensor_fit,
                                cd.sensor_width, cd.sensor_height, aspect)


def signature(cam_obj, scene):
    cd = cam_obj.data
    mw = cam_obj.matrix_world
    r = scene.render
    w, h = image_size(photo(cd))
    fit = {'AUTO': 0.0, 'HORIZONTAL': 1.0, 'VERTICAL': 2.0}.get(cd.sensor_fit, 3.0)
    return ([v for row in mw[:3] for v in row] +
            [cd.lens, cd.shift_x, cd.shift_y, fit, cd.sensor_width, cd.sensor_height,
             1.0 if cd.type == 'PERSP' else 0.0,
             float(r.resolution_x), float(r.resolution_y), r.pixel_aspect_x, r.pixel_aspect_y,
             float(w), float(h)])


def remember(cam_obj, scene):
    """The lines now belong to the camera as it is."""
    state.signatures[key(cam_obj)] = signature(cam_obj, scene)


def origin_visible(cam_obj, scene):
    return M.origin_rel(current_state(cam_obj, scene)) is not None


def blender_values(cam_obj, st):
    """(Blender values of a camera state, error message or None when Blender
    cannot take them)."""
    cd = cam_obj.data
    b = M.blender_settings(st, cd.sensor_fit, cd.sensor_width, cd.sensor_height)
    if not all(math.isfinite(v) for v in (b['lens'], b['shift_x'], b['shift_y'])):
        return b, 'invalid result'
    if not (LENS_RANGE[0] <= b['lens'] <= LENS_RANGE[1]):
        return b, f'focal length {b["lens"]:.2f} mm is outside what Blender supports'
    return b, None


def write_state(cam_obj, scene, st):
    """Write a camera state into Blender. Returns an error message or None."""
    cd = cam_obj.data
    b, msg = blender_values(cam_obj, st)
    if msg:
        return msg
    if cd.type != 'PERSP':
        cd.type = 'PERSP'
    if abs(cd.lens - b['lens']) > 1e-7:
        cd.lens = b['lens']
    if abs(cd.shift_x - b['shift_x']) > 1e-9:
        cd.shift_x = b['shift_x']
    if abs(cd.shift_y - b['shift_y']) > 1e-9:
        cd.shift_y = b['shift_y']
    rot = mathutils.Matrix(b['rotation'])
    scale = cam_obj.matrix_world.to_scale()
    cam_obj.matrix_world = mathutils.Matrix.LocRotScale(mathutils.Vector(b['location']), rot, scale)
    return None


# --------------------------------------------------------------------------
# Lines
# --------------------------------------------------------------------------

def lines_dict(pm):
    return {axis: [(tuple(s.a), tuple(s.b)) for s in getattr(pm, attr)]
            for axis, attr in P.LINE_ATTR.items()}


def write_lines(pm, axis, segs):
    coll = getattr(pm, P.LINE_ATTR[axis])
    while len(coll) > len(segs):
        coll.remove(len(coll) - 1)
    while len(coll) < len(segs):
        coll.add()
    for item, (a, b) in zip(coll, segs):
        if tuple(item.a) != tuple(a):
            item.a = a
        if tuple(item.b) != tuple(b):
            item.b = b


def _write_origin(pm, st):
    """Put the origin point onto the projected world origin (if it is in view)."""
    o = M.origin_rel(st)
    if o is not None and tuple(pm.origin) != o:
        pm.origin = o


def _write_scale_end(pm, st):
    """Put the end of the known length (Scale) where this camera sees it: the
    length stays, and so does its side of the origin (scale_flip: it is kept
    as data because after a big camera change the old end on the photo may
    be on the other side of the moved origin)."""
    if not pm.use_scale:
        return
    sign = -1.0 if pm.scale_flip else 1.0
    tip = M.scale_tip(st, pm.scale_axis, sign * pm.scale_length)
    if tip is not None and tuple(pm.scale_point) != tip:
        pm.scale_point = tip


def ensure_lines(cam_obj, scene):
    """Start: store lines built from the camera if it has none yet."""
    pm = settings_of(cam_obj.data)
    if not pm.started:
        st = current_state(cam_obj, scene)
        for axis in P.AXES:
            write_lines(pm, axis, M.build_segments(st, axis))
        _write_origin(pm, st)
        _write_scale_end(pm, st)
        pm.started = True
        changed()
    remember(cam_obj, scene)
    return pm


def forget_errors(cam_obj):
    """The lines and the camera agree again: no errors, no pending principal point."""
    k = key(cam_obj)
    state.errors.pop(k, None)
    state.pending_pp.pop(k, None)


def clear_lines(cam_obj):
    pm = settings_of(cam_obj.data)
    for attr in P.LINE_ATTR.values():
        getattr(pm, attr).clear()
    pm.started = False
    state.signatures.pop(key(cam_obj), None)
    forget_errors(cam_obj)
    changed()


def sync_lines_from_camera(cam_obj, scene, axes=None, origin=True):
    """camera -> lines: re-aim the stored lines at the camera's vanishing points
    (and move the origin point onto the projected world origin). axes: which
    lines; by default all but those the mode keeps (model.kept_axes)."""
    pm = settings_of(cam_obj.data)
    if not pm.started:
        ensure_lines(cam_obj, scene)
        return
    tool = tool_of(scene)
    if axes is None:
        axes = [a for a in P.AXES if a not in M.kept_axes(tool.mode)]
    st = current_state(cam_obj, scene)
    current = lines_dict(pm)
    for axis in axes:
        segs = current[axis]
        if len(segs) < 2:
            segs = M.build_segments(st, axis)
        else:
            segs = [M.retarget_segment(seg, st.vp_h(M.AXIS_VECTORS[axis]), st.aspect)
                    for seg in segs]
        write_lines(pm, axis, segs)
    if origin:
        _write_origin(pm, st)
    _write_scale_end(pm, st)
    remember(cam_obj, scene)
    # the errors are gone only when the lines the mode solves from now agree
    # with the camera (not when e.g. only the origin point followed it)
    if set(M.used_axes(tool.mode, tool.vp1_axis, tool.vp2_axis)) <= set(axes):
        forget_errors(cam_obj)
    changed()


def solve_lines(cam_obj, scene, cur, pp=None, origin_moved=False, reset_shift=False):
    """model.solve_lines for the stored lines in the scene's mode; nothing is
    written. Returns (origin point, or None when the world origin is not in
    front of the camera, result)."""
    pm = settings_of(cam_obj.data)
    tool = tool_of(scene)
    origin = tuple(pm.origin) if M.origin_rel(cur) is not None else None
    res = M.solve_lines(lines_dict(pm), origin, tool.mode, tool.vp1_axis, tool.vp2_axis, cur,
                        origin_moved=origin_moved and origin is not None,
                        reset_shift=reset_shift, pp=pp, scale=scale_settings(pm))
    return origin, res


def check_lines(cam_obj, scene):
    """Find the errors of the lines as they are, writing nothing: after a load
    or an undo they are not known (errors are runtime only), and lines saved
    while impossible must show their error before they are touched."""
    _origin, res = solve_lines(cam_obj, scene, current_state(cam_obj, scene))
    msg = blender_values(cam_obj, res.state)[1] if res.ok else None
    k = key(cam_obj)
    if not res.ok or msg:
        state.errors[k] = res.errors if not res.ok else [msg]
    else:
        state.errors.pop(k, None)
        state.warnings[k] = res.warnings
    changed()


def mode_switched(scene):
    """A new mode starts without shift (except 3 Points, which computes it):
    the camera is solved from the lines with the principal point at the centre."""
    cam = active_camera(scene)
    if cam is not None and is_started(cam):
        solve_and_apply(cam, scene, reset_shift=True)


def solve_and_apply(cam_obj, scene, origin_moved=False, reset_shift=False, pp_rel=None):
    """lines -> camera. On failure the camera is left untouched and the errors
    are kept in state.errors for the overlay / panel. origin_moved: the origin
    point is being dragged (the camera then moves in its own plane).
    pp_rel: the principal point handle was dragged there (relative); the
    camera is written only if the solve succeeds (Parallel Verticals: only its
    x counts, the point stays on the horizon). If it fails, the point stays
    there (state.pending_pp) and the next solves use it."""
    pm = settings_of(cam_obj.data)
    tool = tool_of(scene)
    cur = current_state(cam_obj, scene)
    k = key(cam_obj)
    if reset_shift or not principal_point_editable(tool):
        pp = M.CENTRE if reset_shift else None
    elif pp_rel is not None:
        pp = S.rel_to_plane(pp_rel, cur.aspect)
        if tool.mode == 'VERTICALS':
            pp = (pp[0], cur.pp[1])
    else:
        pp = state.pending_pp.get(k)
    origin, res = solve_lines(cam_obj, scene, cur, pp, origin_moved, reset_shift)
    err = None if res.ok else res.errors
    if res.ok:
        msg = write_state(cam_obj, scene, res.state)
        if msg:
            err = [msg]
            res.state, res.errors = None, err
    if err:
        state.errors[k] = err
        if pp is not None and tool.mode != 'THIRD_VP':
            state.pending_pp[k] = pp
        else:
            state.pending_pp.pop(k, None)
        changed()
        return res
    forget_errors(cam_obj)
    state.warnings[k] = res.warnings
    if pm.use_scale and not origin_moved:
        # the end was put by the user: which side of the origin it is on
        s = M.scale_measure(res.state, pm.scale_axis, tuple(pm.scale_point))
        if s is not None and (s < 0.0) != pm.scale_flip:
            pm.scale_flip = s < 0.0
    # Lines that were not inputs of this solve follow the new camera; so does the
    # origin point when the camera could not be placed on it. In 1 Point mode
    # the other axes keep their lines where they are, so that going back to a
    # mode that uses them finds them as the user left them; so do the kept axes.
    used = set(M.used_axes(tool.mode, tool.vp1_axis, tool.vp2_axis))
    kept = M.kept_axes(tool.mode)
    rest = [] if tool.mode == 'ONE_VP' else [a for a in P.AXES
                                             if a not in used and a not in kept]
    sync_lines_from_camera(cam_obj, scene, axes=rest,
                           origin=origin is None or res.origin_follows)
    return res


# --------------------------------------------------------------------------
# Scale: a known length from the world origin along an axis
# --------------------------------------------------------------------------

def scale_settings(pm):
    """(axis, end point, length) for model.solve_lines, or None when Scale is off."""
    return (pm.scale_axis, tuple(pm.scale_point), pm.scale_length) if pm.use_scale else None


def _scale_camera(cam_data, scene):
    """The picked, started camera that uses this camera data, or None."""
    cam = active_camera(scene, started=True) if scene else None
    return cam if cam is not None and cam.data == cam_data else None


_scale_quiet = False             # set while the add-on itself writes scale_length


def scale_switched(cam_data, scene):
    """Scale was switched on; the camera does not move. The first time, the
    axis keeps the size it is drawn at and the length is what that is now in
    the scene; after that the length is remembered (also when typed while
    Scale was off) and the end goes where that length is now."""
    global _scale_quiet
    cam = _scale_camera(cam_data, scene)
    pm = settings_of(cam_data)
    if cam is not None and pm.use_scale:
        st = current_state(cam, scene)
        if not pm.scale_length_set:
            _scale_quiet = True
            try:
                pm.scale_length = ORIGIN_AXIS_LENGTH * math.sqrt(sum(c * c for c in st.C))
            finally:
                _scale_quiet = False
            pm.scale_length_set = True
            pm.scale_flip = False
        _write_scale_end(pm, st)
    changed()


def scale_axis_changed(cam_data, scene):
    """Another axis: the same length along it; the camera does not move."""
    cam = _scale_camera(cam_data, scene)
    pm = settings_of(cam_data)
    if cam is not None and pm.use_scale:
        pm.scale_flip = False
        _write_scale_end(pm, current_state(cam, scene))
    changed()


def scale_length_changed(cam_data, scene):
    """A new known length: the camera moves along its line to the origin so the
    end of the axis measures it; the photo, the lines and the points stay."""
    cam = _scale_camera(cam_data, scene)
    pm = settings_of(cam_data)
    if not _scale_quiet:
        pm.scale_length_set = True              # typed by the user: remembered
    if _scale_quiet or cam is None or not pm.use_scale:
        changed()
        return
    new, _err = M.apply_scale(current_state(cam, scene), pm.scale_axis,
                              tuple(pm.scale_point), pm.scale_length)
    if new is not None and not write_state(cam, scene, new):
        remember(cam, scene)
    changed()


def scale_problem(cam_obj, scene):
    """Why the known length cannot be used now, or None."""
    pm = settings_of(cam_obj.data)
    if not pm.use_scale:
        return None
    st = current_state(cam_obj, scene)
    if M.origin_rel(st) is None:
        return 'The world origin is not in front of the camera'
    if not M.scale_axis_usable(st, pm.scale_axis):
        return f'The {pm.scale_axis} axis points almost at the camera: pick another axis'
    return None


# Scene Properties > Units > Length: metres per unit and how Blender writes it
_LENGTH_UNITS = {
    'KILOMETERS': (1000.0, ' km'), 'METERS': (1.0, ' m'), 'CENTIMETERS': (0.01, ' cm'),
    'MILLIMETERS': (0.001, ' mm'), 'MICROMETERS': (1e-6, ' \u00b5m'),
    'MILES': (1609.344, ' mi'), 'FEET': (0.3048, "'"), 'INCHES': (0.0254, '"'),
    'THOU': (0.0000254, ' thou'),
}


def length_text(scene, value):
    """A length (Blender units) written in the scene's length unit, like the
    fields of the panel (Adaptive: the unit Blender picks for the value)."""
    unit = scene.unit_settings
    if unit.system == 'NONE':
        return f'{value:.3f}'.rstrip('0').rstrip('.')
    metres = value * unit.scale_length
    fixed = _LENGTH_UNITS.get(unit.length_unit)
    if fixed is None:
        return bpy.utils.units.to_string(unit.system, 'LENGTH', metres, precision=3,
                                         split_unit=unit.use_separate)
    number = f'{metres / fixed[0]:.3f}'.rstrip('0').rstrip('.')
    return ('0' if number in ('', '-0') else number) + fixed[1]


def principal_point_editable(tool):
    """The principal point (lens shift) can be dragged when Edit Principal
    Point is on, in every mode except 3 Points (which computes it), and only
    while the lines are shown."""
    return bool(tool and tool.edit_principal_point and tool.show_overlay
                and tool.mode != 'THIRD_VP')


def principal_point_toggled():
    """Edit Principal Point was switched: a point left where it gave no
    solution goes back to the camera's shift."""
    state.pending_pp.clear()


def principal_point(cam_obj, st):
    """Where the principal point is (image plane): the camera's lens shift, or
    where it was dragged to while that gives no solution (state.pending_pp)."""
    return state.pending_pp.get(key(cam_obj), st.pp)


def principal_point_rel(cam_obj, scene):
    """Where the principal point is on the photo (relative), or None when it
    cannot be dragged now. It is the camera's lens shift, nothing is stored."""
    tool = tool_of(scene) if scene else None
    if cam_obj is None or not principal_point_editable(tool):
        return None
    st = current_state(cam_obj, scene)
    return S.plane_to_rel(principal_point(cam_obj, st), st.aspect)


def snapshot(cam_obj):
    """Camera and lines, to restore when a transform is cancelled."""
    cd = cam_obj.data
    pm = settings_of(cd)
    k = key(cam_obj)
    return dict(matrix=cam_obj.matrix_world.copy(), lens=cd.lens, shift=(cd.shift_x, cd.shift_y),
                lines=lines_dict(pm), origin=tuple(pm.origin), scale_point=tuple(pm.scale_point),
                errors=state.errors.get(k), pending_pp=state.pending_pp.get(k))


def restore(cam_obj, scene, snap):
    cd = cam_obj.data
    pm = settings_of(cd)
    cam_obj.matrix_world = snap['matrix']
    cd.lens = snap['lens']
    cd.shift_x, cd.shift_y = snap['shift']
    for axis, segs in snap['lines'].items():
        write_lines(pm, axis, segs)
    pm.origin = snap['origin']
    pm.scale_point = snap['scale_point']
    remember(cam_obj, scene)
    forget_errors(cam_obj)
    k = key(cam_obj)
    if snap.get('errors'):                 # back to how it was, errors included
        state.errors[k] = snap['errors']
    if snap.get('pending_pp') is not None:
        state.pending_pp[k] = snap['pending_pp']
    changed()


# --------------------------------------------------------------------------
# Picking the camera in the panel
# --------------------------------------------------------------------------

def camera_picked(scene, context):
    """The camera field of the tool changed: start on that camera and look
    through it, with the render size of its photo (an explicit user action).
    A running Edit Perspective goes on with the new camera.
    Called from a property update: no operators here."""
    from . import interact
    with one_redraw():
        cam = active_camera(scene)
        update_active()
        if cam is not None:
            if scene.camera != cam:
                scene.camera = cam
            if is_started(cam):
                follow_camera(cam, scene)        # it may have moved while not picked
            else:
                ensure_lines(cam, scene)
            if tool_of(scene).mode == 'VERTICALS':
                # the lines are a picture of the camera, but this mode also makes
                # it level: now, as switching to the mode does, not at the first
                # touch of a line
                solve_and_apply(cam, scene)
            area = getattr(context, 'area', None) if context else None
            if area is not None and area.type == 'VIEW_3D':
                rv3d = area.spaces.active.region_3d
                if rv3d is not None and rv3d.view_perspective != 'CAMERA':
                    rv3d.view_perspective = 'CAMERA'
            apply_photo_resolution(scene, cam)
        interact.camera_switched(cam)
        changed()


# --------------------------------------------------------------------------
# Information for the UI
# --------------------------------------------------------------------------

def override_warnings(cam_obj):
    """What will overwrite the edited camera: keyframes, drivers, constraints."""
    out = []
    keyed = driven = False
    for idb in (cam_obj, cam_obj.data):
        ad = idb.animation_data
        if ad is None:
            continue
        if ad.action is not None or any(len(t.strips) for t in ad.nla_tracks):
            keyed = True
        if len(ad.drivers):
            driven = True
    if keyed:
        out.append('The camera is animated: keyframes overwrite the edits when the frame changes')
    if driven:
        out.append('The camera has drivers: driven values overwrite the edits')
    if any(c.enabled for c in cam_obj.constraints):
        out.append('The camera has constraints: they override the edited position and rotation')
    return out


def solution_info(cam_obj, scene):
    st = current_state(cam_obj, scene)
    cd = cam_obj.data
    hx, hy = S.plane_half_extent(st.aspect)
    return {
        'lens': cd.lens,
        # the sensor size the focal length refers to (Blender: Vertical uses the height)
        'sensor': cd.sensor_height if cd.sensor_fit == 'VERTICAL' else cd.sensor_width,
        'sensor_fit': cd.sensor_fit,
        'fov_h': math.degrees(2.0 * math.atan(hx / st.f)),
        'fov_v': math.degrees(2.0 * math.atan(hy / st.f)),
        'shift_x': cd.shift_x,
        'shift_y': cd.shift_y,
        'location': tuple(cam_obj.matrix_world.translation),
        # in the object's own Euler order, like the Rotation fields (world space)
        'rotation': tuple(math.degrees(a) for a in cam_obj.matrix_world.to_euler(
            cam_obj.rotation_mode if len(cam_obj.rotation_mode) == 3 else 'XYZ')),
    }


_hold = 0          # > 0 inside one_redraw(): redraws are collected
_pending = False


@contextmanager
def one_redraw():
    """Merge all redraw requests made inside (one event) into one."""
    global _hold, _pending
    _hold += 1
    try:
        yield
    finally:
        _hold -= 1
        if _hold == 0 and _pending:
            _pending = False
            _tag_now()


def tag_redraw_view3d():
    global _pending
    if _hold:
        _pending = True
        return
    _tag_now()


def _tag_now():
    wm = bpy.context.window_manager
    if wm is None:
        return
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# --------------------------------------------------------------------------
# camera -> lines: follow every change of the matched camera
# --------------------------------------------------------------------------

_busy = False


def check_scene(scene):
    """Re-aim the lines if the matched camera changed since they were last
    matched (by hand, by animation...)."""
    cam = active_camera(scene)
    if cam is not None and is_started(cam):
        follow_camera(cam, scene)


def follow_camera(cam, scene):
    global _busy
    if _busy:
        return
    sig = signature(cam, scene)
    known = state.signatures.get(key(cam))
    if known is None:
        # first look (picked, loaded, undone): the lines and the camera belong
        # together; whether the lines can be solved is not known yet
        state.signatures[key(cam)] = sig
        check_lines(cam, scene)
        return
    if M.signatures_equal(sig, known):
        return
    _busy = True
    try:
        sync_lines_from_camera(cam, scene)
    finally:
        _busy = False


@bpy.app.handlers.persistent
def _on_depsgraph(scene, _depsgraph=None):
    if not state.active:            # nothing picked anywhere: no work at all
        return
    try:
        check_scene(scene)
    except Exception as ex:          # never break Blender from a handler
        print('Perspective Match:', ex)


@bpy.app.handlers.persistent
def _on_reload(*_args):
    """After loading a file, undo or redo, the lines and the camera stored in it
    belong together: the signatures are dropped and the first depsgraph update
    takes them again (follow_camera: first look) instead of re-aiming. They
    cannot be taken here: matrix_world is not evaluated yet (identity after
    loading a file, the camera before the undo after an undo)."""
    state.signatures.clear()
    state.errors.clear()
    state.pending_pp.clear()
    state.warnings.clear()
    state.selection.clear()
    update_active()
    changed()


@bpy.app.handlers.persistent
def _on_load(*_args):
    state.frame_rects.clear()          # the regions of the old file are gone
    _on_reload()


_HANDLERS = (('depsgraph_update_post', _on_depsgraph), ('frame_change_post', _on_depsgraph),
             ('load_post', _on_load), ('undo_post', _on_reload), ('redo_post', _on_reload))


def register():
    for name, fn in _HANDLERS:
        getattr(bpy.app.handlers, name).append(fn)
    bpy.app.timers.register(lambda: _on_reload() and None, first_interval=0.0)


def unregister():
    for name, fn in _HANDLERS:
        handlers = getattr(bpy.app.handlers, name)
        while fn in handlers:
            handlers.remove(fn)
