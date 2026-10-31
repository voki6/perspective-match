# SPDX-License-Identifier: GPL-3.0-or-later
"""Runtime state (never saved in the file)."""

# True when any scene has a camera picked for Perspective Match. The draw and
# depsgraph handlers return at once while it is False (cheap check first).
active = False

# Bumped whenever the lines or the matched camera change; the overlay cache is
# rebuilt only when this (or the view) changes.
version = 0

# Camera "signature" (see camera.signature) the lines were last matched to,
# per camera (camera.key). Tells "the camera changed" from "we wrote the camera".
signatures = {}

# Camera frame rectangle per 3D view region, in region pixels:
# region.as_pointer() -> (left, bottom, right, top). Filled while drawing,
# entries of regions that are gone are dropped (overlay.store_rect).
frame_rects = {}

# The running "Edit Perspective" session, or None. A dict with:
#   'camera'       name of the camera object being edited
#   'region'       as_pointer() of the 3D view region it runs in
#   'hover'        handle key under the mouse (or None)
#   'box'          (x0, y0, x1, y1) box selection in region pixels (or None)
#   'transform'    short description of the running transform (or None)
#   'snap_target'  region position of the point being snapped to (or None)
#   'stop'         set to True to ask the session to end
session = None

# Selected handle keys of the edit session (selection is not saved)
selection = set()

# Last exception raised while drawing the overlay (for debugging), or None
last_draw_error = None

# Last solve result per camera (key: camera.key(), the object name; so are the
# signatures): errors are shown in the overlay and the panel until the next
# successful solve or camera change.
errors = {}
warnings = {}

# Where the principal point handle was dragged to (image plane), per camera,
# while that position gives no solution: the camera keeps its last good shift
# (as it does for the lines) but the point stays where the user put it.
# Dropped together with the errors (camera.forget_errors).
pending_pp = {}

# (text, time) of a short notice shown over the photo, or None (camera.notify)
notice = None
