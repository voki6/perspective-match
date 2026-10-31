# Perspective Match — design decisions

Why the add-on works the way it does. `AGENTS.md` has the rules, the module
map and how to test; `docs/MATH.md` has the maths. This file describes the
current design; the history of changes is in the version control log.

## Principles

* **The camera is the single source of truth; the lines are an interactive
  picture of it.** Everything else follows from this.
* **Light and standard.** The add-on behaves like a built-in Blender tool,
  reads the user's settings (keymap, theme, snapping, units) instead of
  copying them, and adds nothing outside its own Sidebar tab.
* **Nothing dangling.** A mode is either on or off; leaving the camera view
  ends Edit Perspective instead of leaving it paused somewhere.
* **Refuse rather than be wrong.** Detect Lines changes nothing when the photo
  does not give a reliable camera and says why.

## Data

* Stored on the camera data (`Camera.perspective_match`): whether the match
  is started, the lines of each axis (segment ends, relative coordinates), the
  origin point and the Scale settings. Each camera has its own match.
* The photo is not stored separately: it is the camera's first usable
  background image. Loading another photo for the same camera replaces the
  image in that entry (Blender cannot reorder background images, so an added
  entry would stay behind the old photo).
* Tool settings live on the scene (`Scene.perspective_match`): the picked
  camera, mode, axes, display toggles, Edit Principal Point.
* Selection, camera signatures, errors, notices and the edit session are
  runtime only (`state.py`).

## Picking the camera

* The camera is picked in a plain object field at the top of the panel, like
  the Camera field in the Scene properties (`template_ID` does not fit: it
  shows the data block's user count and a copy button). Picking starts the
  match: lines are built, it becomes the scene camera, the view goes through
  it and the render size is set from its photo. There are no Start / Stop
  buttons; clearing the field switches everything off.
* While no camera is picked the add-on does no work: `state.active` is the
  first check in every handler.
* Picking another camera while Edit Perspective runs switches the session to
  it; clearing the field ends the session; picking a camera while the session
  is off does not start it. The **+** (New Camera from Photo) always starts
  Edit Perspective after loading.

## Camera ↔ lines

* **Camera → lines**: depsgraph and frame-change handlers compare the camera's
  signature (matrix, lens, shift, sensor, render size, photo size) with the
  remembered one; if it differs, the lines are re-aimed at the new vanishing
  points (rotated about their middles, lengths kept).
* **Lines → camera**: dragging lines solves the camera and writes it, then
  remembers the new signature — so the handler does not re-aim the lines that
  were just moved, and the first touch of a line never makes the camera jump.
* After load, undo and redo the signatures are simply taken again: the lines
  and the camera in the file agree.
* An impossible configuration (e.g. both vanishing points on one side of the
  principal point) keeps the camera in its last good state; the lines, and the
  principal point, stay where they were put, and the camera follows again as
  soon as the configuration is valid.
* Lines of the axes that the mode does not use are re-aimed at the solved
  camera — except in 1 Point, which leaves them alone so they are still in
  place when switching back to another mode.

## Modes and lens shift

* Lens shift (the principal point) is reset to the image centre when the
  **mode** is switched, and kept while lines are moved. 3 Points computes it
  from the three vanishing points.
* **Parallel Verticals is a level camera**: Rotation X exactly 90°, Y exactly
  0°. The horizon is fitted to all lines of both horizontal axes at once, so
  the two vanishing points lie on one horizontal line; Shift Y does the tilt.
  The solver's original "verticals" principal point mode stays for the parity
  tests but is not used.
* The axis fields show only what the mode needs: 2 Points — one Directions
  field (XY / XZ / YZ), 1 Point — one Axis field, 3 Points and Parallel
  Verticals — none (Parallel Verticals always uses X and Y).
* **Edit Principal Point** (an icon toggle next to Mode, off by default,
  greyed out in 3 Points) makes the principal point a draggable handle, without
  a mode list or stored data. In 2 / 1 Point
  it moves freely; in Parallel Verticals it slides along the horizon. The
  handle does not write the shift itself: the position goes into the next
  solve, and the camera is written only if that solve succeeds. Its crosshair
  turns pink when it is away from the mode's default.

## Camera position

* Moving lines keeps the camera's **distance to the world origin**.
  Keeping the height instead was tried and removed: with the origin point
  near the horizon the line of sight runs almost along the floor, a fraction
  of a degree sent the camera kilometres away, and far from the origin
  Blender starts to jitter.
* Dragging the origin point moves the camera in its own image plane.
* A new camera is 1.6 m high in scene units, so scenes in millimetres work.
* **Reset Camera Position** gives the same standard place wherever the camera
  was (pressing it again changes nothing): 1.6 m high, origin on the floor in
  the middle column halfway between the horizon and the bottom edge; rotation,
  lens, shift and lines are unchanged. With Scale on it keeps the scaled
  distance instead of 1.6 m.

## Scale

* A known length along an axis **from the origin** (off by default, per
  camera): no floor needed and nothing extra in the frame, unlike a free
  two-point reference distance.
* The axes at the origin always show their true directions; the known length
  is a separate measuring line over them, like Blender's Measure tool (light
  dashed line, dark outline, ticks at both ends, the length at its middle).
  Replacing the axis by the ruler was tried: a ruler on the negative side made
  the axis look flipped.
* Changing the length or dragging the end moves the camera along its line to
  the origin: the photo stays, the size of the world changes. Switching Scale
  on or changing the axis does not move the camera. The first time, the length
  is what the axis measures now; after that it is remembered.
* Which side of the origin the ruler is on is stored and changes only when the
  end is dragged across the origin. Deriving it from the old end after the
  camera moved drew the Z ruler downwards after Reset Camera Position.
* Lengths are shown in the scene's length unit, as Blender's fields do.

## Detect Lines

* numpy only (bundled with Blender), imported when the button is pressed.
* Pixels come from the GPU texture Blender already uses for the camera
  background, drawn at 1200 px into an offscreen buffer and read back (~20 ms,
  ~15 MB, also on integrated graphics), through Blender's linear → sRGB image
  shader so the values match the image file. Without a GPU (background mode)
  a scaled temporary copy is used; that copy needs ~100 MB for a 24 MP photo,
  which is why it is only the fallback.
* Directions are found together as **three perpendicular directions**
  (Manhattan frame) out of up to six vanishing-point candidates, so canopies,
  trusses and wires are not taken for directions of the building.
* Decisions use the statistical error (bootstrap, parallel-or-not tests on the
  residuals), not pixel thresholds. Every mode needs the focal length stable
  within 5 %; looser limits answered a few more photos but let wrong focal
  lengths through.
* The camera is then fitted to **all** segments of the three directions at once
  (exactly perpendicular directions). Compared with solving each vanishing
  point on its own, this lowered the median direction error on the York Urban
  Database from 1.54° to 1.23° and raised focal lengths within 5 % from 62 %
  to 73 %.
* Two lines per direction are written — long and far apart, so the vanishing
  point is sharp and they are easy to adjust by hand — turned about their
  middles to pass exactly through the fitted vanishing points. Two such lines
  give the same camera as any number.
* The camera is put at the standard place (as Reset Camera Position), so it
  never runs off. One undo step.
* Refusal reasons name the check that failed: no level horizon square to the
  verticals, the focal length not reliable (with its spread in %), only one
  converging direction, nothing converges, directions that cannot be
  perpendicular.
* 1 Point keeps the camera's focal length (the photo cannot give it) and does
  not take the horizon tilt from parallel lines.

## Render resolution and messages

* The render size changes only on explicit actions: loading a photo, picking
  the camera, **Use Photo Size** (shown only when the aspects differ).
* Operators are never called from property update callbacks; the plain
  function is called instead.
* Short notices appear over the photo in the viewport, bottom left, for a few
  seconds (and operators also report in the status bar). They used to be in
  the panel, which made it jump as they appeared and disappeared.

## Integration with Blender

* Keys come from the user's keymap: Node Editor (select, box, move / rotate /
  scale), Transform Modal Map (confirm, cancel, axis, snap); the exit key is
  the Edit Mode toggle (Tab).
* Snapping is the scene's magnet; Ctrl inverts it, as in Blender. The target
  is the point under the cursor, the source follows Snap Target (Closest /
  Center / Median / Active), the rotation step is the scene's angle increment.
* Colours come from the theme (axes, selection), the font from the UI style.
  The mode and the current transform are shown in the area header, key hints
  in the status bar (only while the mouse is over the edited view).
* Lines and points are drawn with a tiny own shader (`draw2d.py`): Blender's
  built-in polyline shaders look stepped on Apple Metal (brightness along a
  shallow line varies 19–37 %, ours 4–5 %) and its point shader draws squares.
* Navigation (MMB, wheel, trackpad, NDOF) always passes through. Leaving the
  camera view ends Edit Perspective.
* Other UI in front (a maximized file browser, the render view, a temporary
  screen, another window) **suspends** Edit Perspective instead of ending it:
  every event passes through, the hints are hidden, the button stays pressed.
  A maximized file browser lives in the same window, so the modal handler
  would otherwise get its clicks.
* The photo is picked with the standard file browser, as in Image ▸ Open.

## Panel

Sidebar ▸ Perspective: the Camera field with **+**, Edit Perspective, the
photo; **Lines** (mode with the Edit Principal Point toggle, the axis field
the mode needs, Detect Lines, Reset Shift, Reset Camera Position, Rebuild
Lines); **Extra Lines** (collapsed, − / + per axis, the hint below them —
Blender shows no tooltips on panel headers); **Scale** (collapsed); **Camera
Info**; **Display** (collapsed: Lines, Horizon).
