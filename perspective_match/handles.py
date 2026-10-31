# SPDX-License-Identifier: GPL-3.0-or-later
"""The draggable points ("handles") of the lines of a camera.

A handle is identified by a key (group, index, end):
    ('X' | 'Y' | 'Z', i, 'a' | 'b')   end of line i parallel to that world axis
    ('origin', 0, None)               the world origin
    ('scale', 0, None)                the end of the known length (Scale on)
Keys stay valid as long as no lines are added or removed. Never keep bpy
references between events; keep keys and look the data up again.

The selection is runtime state (state.selection), it is not saved.
Which groups are visible (editable) depends on the scene tool settings (mode,
axes, Lines on); the origin point only when the world origin is in front of
the camera (origin_visible, passed in by the caller).
"""

from . import model as M
from . import state
from .props import LINE_ATTR


def visible_line_groups(tool):
    return M.used_axes(tool.mode, tool.vp1_axis, tool.vp2_axis)


def segments_of(props, group):
    return list(getattr(props, LINE_ATTR[group]))


def iter_segments(props, tool):
    """(group, index, segment property group) of the editable lines: none while
    the lines are hidden (tool.show_overlay off), so hidden points can neither be
    picked nor moved."""
    if not tool.show_overlay:
        return
    for group in visible_line_groups(tool):
        for i, seg in enumerate(segments_of(props, group)):
            yield group, i, seg


def iter_handle_keys(props, tool, origin_visible=True):
    for group, i, _seg in iter_segments(props, tool):
        yield (group, i, 'a')
        yield (group, i, 'b')
    if origin_visible and tool.show_overlay:
        yield ('origin', 0, None)
        if props.use_scale:
            yield ('scale', 0, None)


def get_co(props, key):
    group, index, end = key
    if group == 'origin':
        return tuple(props.origin)
    if group == 'scale':
        return tuple(props.scale_point)
    coll = getattr(props, LINE_ATTR[group], None)
    if coll is None or not 0 <= index < len(coll):
        return None
    return tuple(getattr(coll[index], end))


def set_co(props, key, co):
    group, index, end = key
    if group == 'origin':
        props.origin = co
        return
    if group == 'scale':
        props.scale_point = co
        return
    coll = getattr(props, LINE_ATTR[group])
    if 0 <= index < len(coll):
        setattr(coll[index], end, co)


def is_selected(key):
    return key in state.selection


def set_selected(key, selected):
    if selected:
        state.selection.add(key)
    else:
        state.selection.discard(key)


def deselect_all():
    state.selection.clear()


def selected_keys(props, tool, origin_visible=True):
    return [k for k in iter_handle_keys(props, tool, origin_visible) if k in state.selection]
