# SPDX-License-Identifier: GPL-3.0-or-later
"""Operators. They work on the camera picked in the panel (camera.active_camera)."""

import math
import os

import bpy
import mathutils
from bpy.props import BoolProperty, EnumProperty, StringProperty

from . import camera
from . import handles as H
from . import model as M
from . import props as P

# New camera, in metres (converted with the scene's unit scale)
NEW_CAMERA_HEIGHT = 1.6
NEW_CAMERA_LOCATION = (6.5, -6.5, NEW_CAMERA_HEIGHT)
NEW_CAMERA_TARGET = (0.0, 0.0, 0.6)


def metres(scene, value):
    """Blender units for a length in metres (scenes in mm have scale_length 0.001)."""
    scale = scene.unit_settings.scale_length or 1.0
    return value / scale


def apply_photo_resolution(op, scene, cam_obj):
    """Render resolution from the camera's photo (notice in the camera view + status bar)."""
    text = camera.apply_photo_resolution(scene, cam_obj)
    if text:
        op.report({'INFO'}, text)
    return text is not None


def set_background(cam_data, image):
    """Make `image` the camera's photo (camera.photo: its first usable background
    image). A new photo replaces the current one in the same entry (keeping its
    opacity and depth; background images cannot be reordered, so an added entry
    would stay behind the old photo); a camera without a photo gets a new entry.
    The fitting is forced to Stretch, so the photo always fills the camera frame
    exactly."""
    cam_data.show_background_images = True
    current = camera.photo(cam_data)
    bg = next((b for b in cam_data.background_images
               if current is not None and b.source == 'IMAGE' and b.image == current), None)
    if bg is None:
        bg = cam_data.background_images.new()
        bg.display_depth = 'BACK'
        bg.alpha = 1.0
    bg.source = 'IMAGE'
    bg.image = image
    bg.frame_method = 'STRETCH'
    bg.show_background_image = True
    return bg


def view_through_camera(context):
    area = context.area
    if area is None or area.type != 'VIEW_3D':
        screen = context.screen
        area = next((a for a in screen.areas if a.type == 'VIEW_3D'), None) if screen else None
    if area is not None:
        rv3d = area.spaces.active.region_3d
        if rv3d is not None and rv3d.view_perspective != 'CAMERA':
            rv3d.view_perspective = 'CAMERA'


def new_camera(context):
    """A camera 1.6 m above the ground (in the scene's units), looking at the origin."""
    scene = context.scene
    cam_data = bpy.data.cameras.new('Perspective Match')
    cam_obj = bpy.data.objects.new('Perspective Match', cam_data)
    context.collection.objects.link(cam_obj)
    loc = mathutils.Vector([metres(scene, v) for v in NEW_CAMERA_LOCATION])
    direction = mathutils.Vector([metres(scene, v) for v in NEW_CAMERA_TARGET]) - loc
    rot = direction.to_track_quat('-Z', 'Y').to_matrix().to_4x4()
    cam_obj.matrix_world = mathutils.Matrix.Translation(loc) @ rot
    return cam_obj


def finish_setup(op, context, cam_obj, image):
    scene = context.scene
    set_background(cam_obj.data, image)
    apply_photo_resolution(op, scene, cam_obj)
    for obj in context.selected_objects:
        obj.select_set(False)
    cam_obj.select_set(True)
    context.view_layer.objects.active = cam_obj
    tool = scene.perspective_match
    if tool.camera != cam_obj:
        tool.camera = cam_obj            # starts it and looks through it
    else:
        camera.sync_lines_from_camera(cam_obj, scene)
    view_through_camera(context)


class PM_OT_setup_from_image(bpy.types.Operator):
    """Match the scene camera to a photo: the photo becomes the camera background, the
    render size is set to the photo, lines are drawn from the camera (a new camera is
    placed 1.6 m above the ground)"""
    bl_idname = 'perspective_match.setup_from_image'
    bl_label = 'Set Up from Photo'
    bl_options = {'REGISTER', 'UNDO'}

    filepath: StringProperty(subtype='FILE_PATH', options={'SKIP_SAVE'})
    # set by scripts / buttons, not shown in the file browser
    image_name: StringProperty(options={'HIDDEN', 'SKIP_SAVE'})
    new_camera: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    # the same file browser as Image > Open
    filter_image: BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})
    filter_movie: BoolProperty(default=False, options={'HIDDEN', 'SKIP_SAVE'})
    filter_folder: BoolProperty(default=True, options={'HIDDEN', 'SKIP_SAVE'})
    relative_path: BoolProperty(name='Relative Path', default=True,
                                description='Select the file relative to the blend file')

    @classmethod
    def description(cls, context, properties):
        if properties.new_camera:
            return ('New Camera from Photo: a new camera 1.6 m above the ground with the photo as '
                    'its background, render size from the photo')
        return ' '.join((cls.__doc__ or '').split())

    def invoke(self, context, event):
        from . import interact
        self._area_ptr = interact.area_pointer(context)
        if self.image_name or self.filepath:
            return self.execute(context)
        # A running Edit Perspective stays on: while the browser is in front it
        # lets every event through (see PM_OT_edit._suspended)
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        from . import interact
        result = self._load_and_set_up(context)
        if self.new_camera and 'FINISHED' in result and not interact.is_running():
            # a new camera from the "+": straight into Edit Perspective
            interact.start_session(getattr(self, '_area_ptr', None) or interact.area_pointer(context))
        return result

    def _load_and_set_up(self, context):
        if self.image_name:
            image = bpy.data.images.get(self.image_name)
            if image is None:
                self.report({'ERROR'}, f'No image named "{self.image_name}"')
                return {'CANCELLED'}
        else:
            path = bpy.path.abspath(self.filepath)
            if not os.path.isfile(path):
                self.report({'ERROR'}, 'Pick a photo file')
                return {'CANCELLED'}
            try:
                image = bpy.data.images.load(path, check_existing=True)
            except RuntimeError as ex:
                self.report({'ERROR'}, f'Cannot load the photo: {ex}')
                return {'CANCELLED'}
            if self.relative_path and bpy.data.filepath:
                try:
                    image.filepath = bpy.path.relpath(path)
                except ValueError:            # another drive on Windows
                    pass
        w, h = camera.image_size(image)
        if w <= 0 or h <= 0:
            self.report({'ERROR'}, 'The photo has no pixels (cannot read it)')
            return {'CANCELLED'}
        cam_obj = None if self.new_camera else (camera.active_camera(context.scene) or
                                                camera.scene_camera(context.scene))
        if cam_obj is None:
            cam_obj = new_camera(context)
        finish_setup(self, context, cam_obj, image)
        return {'FINISHED'}


class PM_OT_setup_from_background(bpy.types.Operator):
    """Use the photo the picked camera already shows as background: render size from the
    photo and lines from the camera (no need to load it again)"""
    bl_idname = 'perspective_match.setup_from_background'
    bl_label = 'Use Background Photo'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = camera.active_camera(context.scene)
        return obj is not None and camera.photo(obj.data) is not None

    def execute(self, context):
        obj = camera.active_camera(context.scene)
        finish_setup(self, context, obj, camera.photo(obj.data))
        return {'FINISHED'}


class PM_OT_fit_render(bpy.types.Operator):
    """Set the render resolution to the size of the photo in pixels (use Resolution % for a
    smaller or bigger render)"""
    bl_idname = 'perspective_match.fit_render'
    bl_label = 'Use Photo Size'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = camera.active_camera(context.scene)
        return obj is not None and camera.photo(obj.data) is not None

    def execute(self, context):
        obj = camera.active_camera(context.scene)
        if camera.image_size(camera.photo(obj.data))[0] <= 0:
            self.report({'ERROR'}, 'The photo has no pixels')
            return {'CANCELLED'}
        apply_photo_resolution(self, context.scene, obj)
        return {'FINISHED'}


AXIS_ITEMS = [(a, a, '') for a in P.AXES]


class PM_OT_line_add(bpy.types.Operator):
    """Add a line along this axis; it starts on the camera's vanishing point, so the
    camera does not move"""
    bl_idname = 'perspective_match.line_add'
    bl_label = 'Add Line'
    bl_options = {'REGISTER', 'UNDO'}

    axis: EnumProperty(items=AXIS_ITEMS)

    @classmethod
    def poll(cls, context):
        return camera.active_camera(context.scene) is not None

    def execute(self, context):
        obj = camera.active_camera(context.scene)
        scene = context.scene
        camera.ensure_lines(obj, scene)
        pm = obj.data.perspective_match
        coll = getattr(pm, P.LINE_ATTR[self.axis])
        st = camera.current_state(obj, scene)
        mids = [((s.a[0] + s.b[0]) / 2, (s.a[1] + s.b[1]) / 2) for s in coll]
        if len(mids) >= 2:
            # between the two lines that are furthest apart, a bit off the middle
            best = max(((i, j) for i in range(len(mids)) for j in range(i + 1, len(mids))),
                       key=lambda ij: math.dist(mids[ij[0]], mids[ij[1]]))
            m0, m1 = mids[best[0]], mids[best[1]]
            t = 0.5 + 0.12 * ((len(coll) % 3) - 1)
            mid = (m0[0] + (m1[0] - m0[0]) * t, m0[1] + (m1[1] - m0[1]) * t)
            length = sum(math.dist(s.a, s.b) for s in coll) / len(coll)
        else:
            mid, length = (0.5, 0.5), 0.25
        dummy = ((mid[0] - length / 2, mid[1]), (mid[0] + length / 2, mid[1]))
        a, b = M.retarget_segment(dummy, st.vp_h(M.AXIS_VECTORS[self.axis]), st.aspect)
        seg = coll.add()
        seg.a, seg.b = a, b
        H.deselect_all()
        i = len(coll) - 1
        H.set_selected((self.axis, i, 'a'), True)
        H.set_selected((self.axis, i, 'b'), True)
        camera.changed()
        return {'FINISHED'}


class PM_OT_line_remove(bpy.types.Operator):
    """Remove the selected lines along this axis (or the last one); at least two stay"""
    bl_idname = 'perspective_match.line_remove'
    bl_label = 'Remove Line'
    bl_options = {'REGISTER', 'UNDO'}

    axis: EnumProperty(items=AXIS_ITEMS)

    @classmethod
    def poll(cls, context):
        return camera.active_camera(context.scene, started=True) is not None

    def execute(self, context):
        obj = camera.active_camera(context.scene)
        pm = obj.data.perspective_match
        coll = getattr(pm, P.LINE_ATTR[self.axis])
        if len(coll) <= 2:
            self.report({'INFO'}, 'An axis needs at least two lines')
            return {'CANCELLED'}
        selected = [i for i in range(len(coll))
                    if H.is_selected((self.axis, i, 'a'))
                    or H.is_selected((self.axis, i, 'b'))] or [len(coll) - 1]
        for i in sorted(selected, reverse=True):
            if len(coll) <= 2:
                break
            coll.remove(i)
        H.deselect_all()
        camera.changed()
        # the remaining lines may point at a slightly different vanishing point
        tool = context.scene.perspective_match
        if self.axis in M.used_axes(tool.mode, tool.vp1_axis, tool.vp2_axis):
            camera.solve_and_apply(obj, context.scene)
        return {'FINISHED'}


class PM_OT_reset_shift(bpy.types.Operator):
    """Put the principal point back in the middle of the photo (lens shift 0; in Parallel
    Verticals Shift X 0) and solve the camera again from the lines"""
    bl_idname = 'perspective_match.reset_shift'
    bl_label = 'Reset Shift'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        tool = camera.tool_of(context.scene)
        return (camera.active_camera(context.scene, started=True) is not None
                and tool.mode != 'THIRD_VP')

    def execute(self, context):
        obj = camera.active_camera(context.scene, started=True)
        res = camera.solve_and_apply(obj, context.scene, reset_shift=True)
        if not res.ok:
            self.report({'WARNING'}, '; '.join(res.errors))
            return {'CANCELLED'}
        return {'FINISHED'}


def standard_position(obj, scene, st):
    """Put st (model.CamState) at the standard place of Reset Camera Position:
    1.6 m above the ground, the world origin in the middle column halfway
    between horizon and bottom (10 m ahead without ground). With Scale on the
    distance to the origin stays (the known length sets the size)."""
    dist = math.sqrt(sum(c * c for c in st.C))
    st.C = M.position_for_origin(st, metres(scene, NEW_CAMERA_HEIGHT), metres(scene, 10.0))
    if obj.data.perspective_match.use_scale and dist > 0.0:
        k = dist / math.sqrt(sum(c * c for c in st.C))
        st.C = tuple(k * c for c in st.C)


class PM_OT_reset_position(bpy.types.Operator):
    """Put the camera back at its standard place: 1.6 m above the ground with the world
    origin in the middle of the photo below the horizon (with Scale on, the size of the
    scene stays instead of the 1.6 m). Rotation, focal length and shift stay, so the lines
    do not change"""
    bl_idname = 'perspective_match.reset_position'
    bl_label = 'Reset Camera Position'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return camera.active_camera(context.scene, started=True) is not None

    def execute(self, context):
        scene = context.scene
        obj = camera.active_camera(scene, started=True)
        st = camera.current_state(obj, scene)
        standard_position(obj, scene, st)
        msg = camera.write_state(obj, scene, st)
        if msg:
            self.report({'WARNING'}, msg)
            return {'CANCELLED'}
        # only the origin point follows; the lines stay (the vanishing points did not move)
        camera.sync_lines_from_camera(obj, scene, axes=(), origin=True)
        return {'FINISHED'}


def photo_pixels(image, size):
    """The photo at most `size` pixels on its longer side, as a float32 array
    (rows from the top, RGBA). Drawn small by the GPU from the texture Blender
    already shows as the camera background (a few MB); without a GPU (blender -b)
    or with a texture limited below `size` (Preferences > Viewport > Texture
    Limit Size) a temporary copy of the image is scaled instead, then removed."""
    import numpy as np
    w, h = image.size
    if w <= 0 or h <= 0:
        return None
    k = max(1.0, max(w, h) / float(size))
    sw, sh = max(1, round(w / k)), max(1, round(h / k))
    if not bpy.app.background:
        import gpu
        from gpu_extras.batch import batch_for_shader
        tex = gpu.texture.from_image(image)
        if tex.width >= sw and tex.height >= sh:
            off = gpu.types.GPUOffScreen(sw, sh)
            try:
                with off.bind():
                    fb = gpu.state.active_framebuffer_get()
                    fb.clear(color=(0.0, 0.0, 0.0, 0.0))
                    with gpu.matrix.push_pop():
                        gpu.matrix.load_matrix(mathutils.Matrix.Identity(4))
                        gpu.matrix.load_projection_matrix(mathutils.Matrix.Identity(4))
                        # the texture holds linear values; image.pixels (the
                        # fallback below) and the photo as seen are sRGB
                        shader = gpu.shader.from_builtin('IMAGE_SCENE_LINEAR_TO_REC709_SRGB')
                        batch = batch_for_shader(shader, 'TRI_FAN', {
                            'pos': ((-1, -1), (1, -1), (1, 1), (-1, 1)),
                            'texCoord': ((0, 0), (1, 0), (1, 1), (0, 1))})
                        shader.bind()
                        shader.uniform_sampler('image', tex)
                        batch.draw(shader)
                    buf = fb.read_color(0, 0, sw, sh, 4, 0, 'FLOAT')
            finally:
                off.free()
            buf.dimensions = sw * sh * 4
            return np.array(buf, dtype=np.float32).reshape(sh, sw, 4)[::-1]
    tmp = image.copy()
    try:
        tmp.scale(sw, sh)
        px = np.empty(sw * sh * 4, np.float32)
        tmp.pixels.foreach_get(px)
    finally:
        bpy.data.images.remove(tmp)
    return px.reshape(sh, sw, 4)[::-1]


DETECT_MODE_NAMES = {'TWO_VP': '2 Points', 'VERTICALS': 'Parallel Verticals',
                     'THIRD_VP': '3 Points', 'ONE_VP': '1 Point'}


class PM_OT_detect_lines(bpy.types.Operator):
    """Finds straight edges on the photo (buildings, interiors) and puts the lines on
    them. The result can be adjusted by hand. Does not work on photos without straight
    edges"""
    bl_idname = 'perspective_match.detect_lines'
    bl_label = 'Detect Lines'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = camera.active_camera(context.scene, started=True)
        return obj is not None and camera.photo(obj.data) is not None

    def execute(self, context):
        from . import detect                    # only when pressed
        scene = context.scene
        obj = camera.active_camera(scene, started=True)
        tool = camera.tool_of(scene)
        pixels = photo_pixels(camera.photo(obj.data), detect.WORK_SIZE)
        if pixels is None:
            return self._refuse('the photo has no pixels')
        det = detect.detect(pixels)
        del pixels
        if not det.ok:
            return self._refuse(det.reason)
        current = camera.current_state(obj, scene)
        plan = detect.plan(det, current)
        if plan is None:
            return self._refuse('the lines found do not give a camera')
        mode, a1, a2, lines, st = plan
        standard_position(obj, scene, st)       # a fresh start: like Reset Camera Position
        msg = camera.write_state(obj, scene, st)
        if msg:
            return self._refuse(msg)
        pm = camera.settings_of(obj.data)
        for axis, segs in lines.items():
            camera.write_lines(pm, axis, segs)
        H.deselect_all()
        # lines of the directions not found, the origin point and the Scale end
        # follow the new camera
        camera.sync_lines_from_camera(obj, scene, axes=[a for a in P.AXES if a not in lines])
        tool.vp1_axis, tool.vp2_axis = a1, a2
        tool.mode = mode                        # solves again from these lines: the same camera
        text = f'Detect Lines: {DETECT_MODE_NAMES[mode]} ({det.reason})'
        if mode == 'ONE_VP':
            text += "; focal length kept from the camera, set it from the photo's EXIF if known"
        self.report({'INFO'}, text)
        return {'FINISHED'}

    def _refuse(self, reason):
        text = f'Detect Lines: nothing changed, {reason}'
        camera.notify(text)
        self.report({'WARNING'}, text)
        return {'CANCELLED'}


class PM_OT_rebuild_lines(bpy.types.Operator):
    """Throw the lines away and draw new ones from the camera"""
    bl_idname = 'perspective_match.rebuild_lines'
    bl_label = 'Rebuild Lines from Camera'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return camera.active_camera(context.scene) is not None

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        obj = camera.active_camera(context.scene)
        H.deselect_all()
        camera.clear_lines(obj)
        camera.ensure_lines(obj, context.scene)
        return {'FINISHED'}


classes = (PM_OT_setup_from_image, PM_OT_setup_from_background, PM_OT_fit_render,
           PM_OT_line_add, PM_OT_line_remove, PM_OT_reset_shift, PM_OT_reset_position,
           PM_OT_detect_lines, PM_OT_rebuild_lines)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
