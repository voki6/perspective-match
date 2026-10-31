# SPDX-License-Identifier: GPL-3.0-or-later
#
# Perspective Match - match a Blender camera to a photo by drawing perspective
# lines, adjustable live in the camera view.
#
# The camera solver is based on fSpy by Per Gantelius
# (https://github.com/stuffmatic/fSpy, GNU GPL v3 or later).

"""Perspective Match: camera matching from perspective lines.

The camera is the single source of truth; the lines over the photo are an
interactive picture of it. Turn the camera by hand and the lines follow; drag
the lines (Sidebar (N) > Perspective > "Edit Perspective") and the camera follows.
"""

if "bpy" in locals():
    import importlib
    for _name in ('solver', 'model', 'state', 'props', 'handles', 'keys', 'camera',
                  'draw2d', 'overlay', 'interact', 'ops', 'ui'):
        if _name in locals():
            importlib.reload(locals()[_name])

import bpy  # noqa: E402,F401

from . import solver    # noqa: E402,F401
from . import model     # noqa: E402,F401
from . import state     # noqa: E402
from . import props     # noqa: E402
from . import handles   # noqa: E402,F401
from . import keys      # noqa: E402,F401
from . import camera    # noqa: E402
from . import draw2d    # noqa: E402,F401
from . import overlay   # noqa: E402
from . import interact  # noqa: E402
from . import ops       # noqa: E402
from . import ui        # noqa: E402

_modules = (props, camera, overlay, interact, ops, ui)


def register():
    for mod in _modules:
        mod.register()


def unregister():
    for mod in reversed(_modules):
        mod.unregister()
    state.session = None
