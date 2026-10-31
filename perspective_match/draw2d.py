# SPDX-License-Identifier: GPL-3.0-or-later
"""Anti-aliased 2D lines and round points for the overlay.

Blender's built-in POLYLINE shader looks stepped on thin lines on some GPUs
(e.g. Apple Metal) and the built-in point shader draws squares, so both are
drawn here with a tiny shader that fades the edge over one pixel: every line
segment and every point is a quad, the fragment shader computes the distance
to the line / to the centre of the point.

All coordinates are region pixels (POST_PIXEL drawing). Colours are given in
sRGB like theme colours; the 3D viewport draws into a linear buffer (Blender's
built-in shaders convert internally), so they are converted here.
"""

import math

import gpu
from gpu_extras.batch import batch_for_shader

_shaders = {}


def _linear(c):
    def f(v):
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return (f(c[0]), f(c[1]), f(c[2]), c[3])

_LINE_VERT = '''
void main()
{
  vDist = side;
  vHalf = halfw;
  vColor = color;
  gl_Position = ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0);
}
'''
_LINE_FRAG = '''
void main()
{
  float a = clamp(vHalf + 0.5 - abs(vDist), 0.0, 1.0);
  fragColor = vec4(vColor.rgb, vColor.a * a);
}
'''
_DISC_VERT = '''
void main()
{
  vOffset = offset;
  vRadius = radius;
  vRim = rim;
  vFill = fill;
  vRimColor = rimcolor;
  gl_Position = ModelViewProjectionMatrix * vec4(pos, 0.0, 1.0);
}
'''
_DISC_FRAG = '''
void main()
{
  float d = length(vOffset);
  float outer = clamp(vRadius - d + 0.5, 0.0, 1.0);
  float inner = clamp(vRadius - vRim - d + 0.5, 0.0, 1.0);
  vec4 c = mix(vRimColor, vFill, inner);
  fragColor = vec4(c.rgb, c.a * outer);
}
'''


def _line_shader():
    sh = _shaders.get('line')
    if sh is None:
        iface = gpu.types.GPUStageInterfaceInfo('pm_line_iface')
        iface.smooth('FLOAT', 'vDist')
        iface.flat('FLOAT', 'vHalf')
        iface.smooth('VEC4', 'vColor')
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', 'ModelViewProjectionMatrix')
        info.vertex_in(0, 'VEC2', 'pos')
        info.vertex_in(1, 'FLOAT', 'side')
        info.vertex_in(2, 'FLOAT', 'halfw')
        info.vertex_in(3, 'VEC4', 'color')
        info.vertex_out(iface)
        info.fragment_out(0, 'VEC4', 'fragColor')
        info.vertex_source(_LINE_VERT)
        info.fragment_source(_LINE_FRAG)
        sh = _shaders['line'] = gpu.shader.create_from_info(info)
    return sh


def _disc_shader():
    sh = _shaders.get('disc')
    if sh is None:
        iface = gpu.types.GPUStageInterfaceInfo('pm_disc_iface')
        iface.smooth('VEC2', 'vOffset')
        iface.flat('FLOAT', 'vRadius')
        iface.flat('FLOAT', 'vRim')
        iface.flat('VEC4', 'vFill')
        iface.flat('VEC4', 'vRimColor')
        info = gpu.types.GPUShaderCreateInfo()
        info.push_constant('MAT4', 'ModelViewProjectionMatrix')
        info.vertex_in(0, 'VEC2', 'pos')
        info.vertex_in(1, 'VEC2', 'offset')
        info.vertex_in(2, 'FLOAT', 'radius')
        info.vertex_in(3, 'FLOAT', 'rim')
        info.vertex_in(4, 'VEC4', 'fill')
        info.vertex_in(5, 'VEC4', 'rimcolor')
        info.vertex_out(iface)
        info.fragment_out(0, 'VEC4', 'fragColor')
        info.vertex_source(_DISC_VERT)
        info.fragment_source(_DISC_FRAG)
        sh = _shaders['disc'] = gpu.shader.create_from_info(info)
    return sh


def lines_batch(segments):
    """segments: [((x0, y0), (x1, y1), width_px, (r, g, b, a)), ...] -> batch or None"""
    pos, side, halfw, color = [], [], [], []
    for a, b, width, col in segments:
        dx, dy = b[0] - a[0], b[1] - a[1]
        n = math.hypot(dx, dy)
        if n < 1e-6:
            continue
        ux, uy = dx / n, dy / n
        nx, ny = -uy, ux
        hw = 0.5 * width
        e = hw + 1.0                         # one more pixel for the soft edge
        a2 = (a[0] - ux * 0.5, a[1] - uy * 0.5)
        b2 = (b[0] + ux * 0.5, b[1] + uy * 0.5)
        quad = [(a2, -e), (a2, e), (b2, -e), (b2, e)]
        for i in (0, 1, 2, 2, 1, 3):
            (px, py), s = quad[i]
            pos.append((px + nx * s, py + ny * s))
            side.append(s)
            halfw.append(hw)
            color.append(_linear(col))
    if not pos:
        return None
    return batch_for_shader(_line_shader(), 'TRIS',
                            {'pos': pos, 'side': side, 'halfw': halfw, 'color': color})


def discs_batch(discs):
    """discs: [((x, y), radius, rim_width, fill_rgba, rim_rgba), ...] -> batch or None"""
    pos, offset, radius, rim, fill, rimcol = [], [], [], [], [], []
    for (cx, cy), r, rw, f, rc in discs:
        e = r + 1.0
        corners = [(-e, -e), (e, -e), (-e, e), (e, e)]
        for i in (0, 1, 2, 2, 1, 3):
            ox, oy = corners[i]
            pos.append((cx + ox, cy + oy))
            offset.append((ox, oy))
            radius.append(r)
            rim.append(rw)
            fill.append(_linear(f))
            rimcol.append(_linear(rc))
    if not pos:
        return None
    return batch_for_shader(_disc_shader(), 'TRIS',
                            {'pos': pos, 'offset': offset, 'radius': radius, 'rim': rim,
                             'fill': fill, 'rimcolor': rimcol})


def draw(batch, kind):
    if batch is None:
        return
    sh = _line_shader() if kind == 'lines' else _disc_shader()
    sh.bind()
    sh.uniform_float('ModelViewProjectionMatrix',
                     gpu.matrix.get_projection_matrix() @ gpu.matrix.get_model_view_matrix())
    batch.draw(sh)


def clear():
    _shaders.clear()
