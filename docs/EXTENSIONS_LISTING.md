# Perspective Match — listing for extensions.blender.org

Package: `dist/perspective_match-1.0.1.zip` (built with
`blender --command extension build`, checked with `extension validate`).

Fields the upload form takes from the manifest: id, name, tagline, version,
type, license (GPL-3.0-or-later), Blender minimum (5.2.0), tags (Camera,
3D View), permission "files" with its reason.

Fields to fill in on the website: description (below), icon, featured image,
at least one screenshot, optionally a website / issue tracker link.

---

## Description (paste into the form, Markdown is supported)

Match a Blender camera to a photo by drawing perspective lines along the edges
you see in it, or let **Detect Lines** place them. The lines give the vanishing
points, the camera is solved from them and updates live while you drag.

**How it works**

* Click **+** next to the Camera field (Sidebar ▸ Perspective) and pick a photo:
  a new camera gets the photo as its background, the render size is set to
  the photo size, and editing starts right away.
* Drag the ends of the lines onto edges in the photo that run along the world
  X, Y and Z axes. Drag the origin point to where the world origin should be.
* The camera is the single source of truth: move or rotate the camera by hand
  and the lines follow it; drag the lines and the camera follows them.
* **Detect Lines** finds straight edges on the photo, chooses the mode and
  places two lines per direction. When the photo does not give a reliable
  camera it changes nothing and says why.
* **Specify Scale**: give the real length of something along an axis from the
  origin; the camera's distance follows.

**Modes**

* **2 Points** – two vanishing points; focal length and rotation are solved.
* **3 Points** – three vanishing points; lens shift is solved as well.
* **Parallel Verticals** – a level camera (Rotation X 90°, Y 0°): verticals stay
  parallel and Shift Y does the tilt, like architectural photography.
* **1 Point** – one vanishing point with the camera's own focal length.

**Editing feels like Blender**

* Select, box select, move / rotate / scale points with your familiar Blender
  shortcuts (Node Editor keys), axis constraints, precision with Shift, snapping to
  other points with the scene snapping toggle (Ctrl inverts it).
* Each edit is one undo step. Viewport navigation is never captured.
* Add extra lines per direction for photos with many short edges
  (tiles, windows, bricks): the solve uses all of them (least squares).
* The principal point can be dragged where the mode allows it (toggle next to
  Mode, off by default).

**Notes**

* The photo is the camera's background image; nothing else is stored apart
  from the line positions on the camera.
* The render resolution is changed only by explicit actions, and a short
  notice in the viewport says so.
* The camera solving maths is based on fSpy by Per Gantelius (GPL-3.0).
* Built with Claude (Anthropic) as the coding assistant: the author set the
  goals and the design and tested everything in Blender; Claude wrote the
  code, the tests and the documentation.

## Permissions

* **Files** – Load the photo the camera is matched to.

## Images to prepare

* Icon: square PNG (256 × 256 works well).
* Featured image: 16:9.
* Screenshots: the lines over a photo in camera view, the Sidebar panel, and a
  before / after of the matched scene.
