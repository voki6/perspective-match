# SPDX-License-Identifier: GPL-3.0-or-later
"""User interface, built from standard Blender pieces only, in one place:
Sidebar (N) > Perspective. The camera field is a plain object field like the
Camera field of the Scene properties (pick a camera to start, its X clears it);
the photo is the camera's background image. Nothing is added anywhere else.
"""


import bpy

from . import camera
from . import handles as H
from . import interact
from . import props as P
from . import state


def _wrapped(layout, text, icon='NONE', alert=False, width=34):
    col = layout.column(align=True)
    col.alert = alert
    words, line, first = text.split(), '', True
    for w in words:
        if line and len(line) + 1 + len(w) > width:
            col.label(text=line, icon=icon if first else 'BLANK1')
            line, first = w, False
        else:
            line = f'{line} {w}' if line else w
    if line:
        col.label(text=line, icon=icon if first else 'BLANK1')


def _picked(context):
    return camera.active_camera(context.scene, started=True)


class _Panel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Perspective'


class PM_PT_main(_Panel, bpy.types.Panel):
    bl_label = 'Perspective Match'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        tool = scene.perspective_match
        layout.use_property_split = True
        layout.use_property_decorate = False
        row = layout.row(align=True)
        row.prop(tool, 'camera')
        op = row.operator('perspective_match.setup_from_image', text='', icon='ADD')
        op.new_camera = True
        layout.use_property_split = False
        obj = camera.active_camera(scene)
        if obj is None:
            return
        for msg in camera.override_warnings(obj):
            _wrapped(layout, msg, icon='ERROR', alert=True)

        running = interact.is_running()
        col = layout.column()
        col.scale_y = 1.75
        col.operator('perspective_match.edit', icon='RESTRICT_SELECT_OFF',
                     text='Stop Editing' if running else 'Edit Perspective', depress=running)

        # The photo is the camera's background image
        photo = camera.photo(obj.data)
        box = layout.box()
        if photo is None:
            box.label(text='No background image on this camera', icon='IMAGE_BACKGROUND')
        else:
            iw, ih = camera.image_size(photo)
            box.label(text=f'{photo.name}  ({iw} × {ih})', icon='IMAGE_BACKGROUND')
            ia, ra = iw / ih, camera.render_aspect(scene)
            if ra > 0 and abs(ra / ia - 1.0) > camera.ASPECT_TOLERANCE:
                r = scene.render
                col = box.column(align=True)
                col.alert = True
                col.label(text=camera.ASPECT_WARNING, icon='ERROR')
                col.label(text=f'Render {r.resolution_x} × {r.resolution_y}', icon='BLANK1')
                box.operator('perspective_match.fit_render', icon='LOOP_BACK')
        box.operator('perspective_match.setup_from_image', icon='FILEBROWSER',
                     text='Other Photo…' if photo else 'Set Up from Photo…')


class PM_PT_lines(_Panel, bpy.types.Panel):
    bl_label = 'Lines'
    bl_parent_id = 'PM_PT_main'

    @classmethod
    def poll(cls, context):
        return _picked(context) is not None

    def draw(self, context):
        layout = self.layout
        tool = context.scene.perspective_match
        layout.use_property_split = True
        layout.use_property_decorate = False
        # the Edit Principal Point toggle sits next to the mode (like the invert
        # toggle next to a vertex group in modifiers); grey in 3 Points, which
        # computes the principal point
        row = layout.row(align=True)
        row.prop(tool, 'mode')
        sub = row.row(align=True)
        sub.enabled = tool.mode != 'THIRD_VP'
        sub.prop(tool, 'edit_principal_point', text='', icon='PIVOT_BOUNDBOX')
        # only what can be chosen in this mode: 3 Points uses all three axes,
        # Parallel Verticals always X and Y
        if tool.mode == 'TWO_VP':
            layout.prop(tool, 'directions')
        elif tool.mode == 'ONE_VP':
            layout.prop(tool, 'vp1_axis')
        if tool.mode == 'VERTICALS':
            _wrapped(layout, 'No tilt or roll: Rotation X 90\u00b0, Y 0\u00b0. '
                             'Vertical lines stay vertical.', icon='INFO')
        layout.use_property_split = False
        layout.operator('perspective_match.detect_lines', icon='VIEWZOOM')
        col = layout.column(align=True)
        if tool.mode != 'THIRD_VP':
            col.operator('perspective_match.reset_shift', icon='LOOP_BACK')
        col.operator('perspective_match.reset_position', icon='OBJECT_ORIGIN')
        col.operator('perspective_match.rebuild_lines', icon='FILE_REFRESH')


EXTRA_LINES_HINT = ('If the photo has no long straight edges but many short ones (tiles, '
                    'windows, bricks), add several lines per direction: the accuracy improves '
                    'noticeably. With long edges two lines are enough.')


class PM_PT_extra_lines(_Panel, bpy.types.Panel):
    bl_label = 'Extra Lines'
    bl_parent_id = 'PM_PT_lines'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _picked(context) is not None

    def draw(self, context):
        layout = self.layout
        tool = context.scene.perspective_match
        pm = _picked(context).data.perspective_match
        col = layout.column(align=True)
        for group in H.visible_line_groups(tool):
            n = len(getattr(pm, P.LINE_ATTR[group]))
            row = col.row(align=True)
            row.label(text=f'{group} lines: {n}')
            sub = row.row(align=True)
            sub.enabled = n > 2
            op = sub.operator('perspective_match.line_remove', text='', icon='REMOVE')
            op.axis = group
            op = row.operator('perspective_match.line_add', text='', icon='ADD')
            op.axis = group
        layout.separator()
        _wrapped(layout, EXTRA_LINES_HINT, icon='INFO')


SCALE_HINT = ('Drag the end of the axis at the origin to the end of something you know '
              'the size of')


class PM_PT_scale(_Panel, bpy.types.Panel):
    """A known length from the world origin along an axis sets the size of the scene"""
    bl_label = 'Scale'
    bl_parent_id = 'PM_PT_main'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _picked(context) is not None

    def draw(self, context):
        layout = self.layout
        obj = _picked(context)
        pm = obj.data.perspective_match
        layout.prop(pm, 'use_scale', text='Specify Scale')
        layout.use_property_split = True
        layout.use_property_decorate = False
        col = layout.column()
        col.active = pm.use_scale
        col.row().prop(pm, 'scale_axis', expand=True)
        col.prop(pm, 'scale_length')
        if pm.use_scale:
            problem = camera.scale_problem(obj, context.scene)
            if problem:
                _wrapped(layout, problem, icon='ERROR', alert=True)
            else:
                _wrapped(layout, SCALE_HINT, icon='INFO')


class PM_PT_camera(_Panel, bpy.types.Panel):
    bl_label = 'Camera Info'
    bl_parent_id = 'PM_PT_main'

    @classmethod
    def poll(cls, context):
        return _picked(context) is not None

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        obj = _picked(context)
        for msg in state.errors.get(camera.key(obj), []):
            _wrapped(layout, msg, icon='CANCEL', alert=True)
        info = camera.solution_info(obj, scene)
        col = layout.column(align=True)
        col.label(text=f'Focal Length: {info["lens"]:.2f} mm')
        col.label(text=f'Sensor: {round(info["sensor"], 2):g} mm ({info["sensor_fit"].title()})')
        col.label(text=f'Field of View: {info["fov_h"]:.1f}° × {info["fov_v"]:.1f}°')
        col.label(text=f'Shift: {info["shift_x"]:.3f}, {info["shift_y"]:.3f}')
        loc = info['location']
        col.label(text='Location: ' + ', '.join(camera.length_text(scene, c) for c in loc))
        col.label(text='Rotation: ' + ', '.join(f'{round(a, 2) + 0.0:.2f}°' for a in info['rotation']))
        for msg in state.warnings.get(camera.key(obj), []):
            _wrapped(layout, msg, icon='ERROR')


class PM_PT_display(_Panel, bpy.types.Panel):
    """What is drawn over the photo (collapsed: rarely changed)"""
    bl_label = 'Display'
    bl_parent_id = 'PM_PT_main'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return _picked(context) is not None

    def draw(self, context):
        tool = context.scene.perspective_match
        col = self.layout.column(align=True)
        col.prop(tool, 'show_overlay')
        col.prop(tool, 'show_horizon')


classes = (PM_PT_main, PM_PT_lines, PM_PT_extra_lines, PM_PT_scale, PM_PT_camera, PM_PT_display)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
