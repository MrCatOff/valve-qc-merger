"""OpenGL 3.3 core renderer for a :class:`~.scene.ModelScene` (Qt wrappers only).

Independent of any widget: :class:`~.viewport.Viewport` drives it on screen,
and :func:`render_offscreen` draws into a framebuffer object (tests, thumbnails).
Skinning happens on the CPU (numpy) and the vertex buffers are streamed each
frame; GoldSource render modes map to: ``masked`` = alpha test on palette
index 255, ``additive`` = ONE/ONE blending without depth writes, drawn after
the opaque batches.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtGui import QImage, QMatrix4x4, QOpenGLContext, QVector3D
from PySide6.QtOpenGL import (
    QOpenGLBuffer,
    QOpenGLShader,
    QOpenGLShaderProgram,
    QOpenGLTexture,
    QOpenGLVertexArrayObject,
)

from valve_qc_merger.studio.model_info import texture_rgba
from valve_qc_merger.studio.scene import ModelScene

GL_FLOAT = 0x1406
GL_TRIANGLES, GL_LINES, GL_POINTS = 0x0004, 0x0001, 0x0000
GL_DEPTH_TEST, GL_BLEND, GL_CULL_FACE = 0x0B71, 0x0BE2, 0x0B44
GL_SCISSOR_TEST, GL_LESS = 0x0C11, 0x0201
GL_PROGRAM_POINT_SIZE = 0x8642
GL_COLOR_BUFFER_BIT, GL_DEPTH_BUFFER_BIT = 0x4000, 0x0100
GL_ONE, GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA = 1, 0x0302, 0x0303
GL_FRONT_AND_BACK, GL_LINE, GL_FILL = 0x0408, 0x1B01, 0x1B02

_MESH_VS = """#version 330 core
layout(location = 0) in vec3 a_pos;
layout(location = 1) in vec3 a_nrm;
layout(location = 2) in vec2 a_uv;
uniform mat4 u_mvp;
out vec3 v_nrm;
out vec2 v_uv;
void main() {
    gl_Position = u_mvp * vec4(a_pos, 1.0);
    v_nrm = a_nrm;
    v_uv = a_uv;
}
"""
_MESH_FS = """#version 330 core
in vec3 v_nrm;
in vec2 v_uv;
uniform sampler2D u_tex;
uniform int u_mode;        // 0 opaque, 1 masked, 2 additive
uniform int u_textured;    // 0/1: an int, set with glUniform1i (see _set_int)
uniform vec3 u_light;
out vec4 frag;
void main() {
    vec4 t = u_textured != 0 ? texture(u_tex, v_uv) : vec4(0.72, 0.72, 0.72, 1.0);
    if (u_mode == 1 && t.a < 0.5) discard;
    float lit = 1.0;
    if (u_mode != 2) {
        float d = dot(normalize(v_nrm), u_light);
        lit = 0.42 + 0.58 * max(d, 0.0);
    }
    frag = vec4(t.rgb * lit, 1.0);
}
"""
_LINE_VS = """#version 330 core
layout(location = 0) in vec3 a_pos;
layout(location = 1) in vec3 a_col;
uniform mat4 u_mvp;
uniform float u_point;
out vec3 v_col;
void main() {
    gl_Position = u_mvp * vec4(a_pos, 1.0);
    gl_PointSize = u_point;
    v_col = a_col;
}
"""
_LINE_FS = """#version 330 core
in vec3 v_col;
out vec4 frag;
void main() { frag = vec4(v_col, 1.0); }
"""

# full-screen vertical gradient behind the model (no vertex buffer: the
# triangle comes from gl_VertexID)
_BG_VS = """#version 330 core
out float v_t;
void main() {
    vec2 p = vec2((gl_VertexID << 1) & 2, gl_VertexID & 2);
    v_t = p.y * 0.5;
    gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0);
}
"""
_BG_FS = """#version 330 core
in float v_t;
uniform vec3 u_top;
uniform vec3 u_bottom;
out vec4 frag;
void main() { frag = vec4(mix(u_bottom, u_top, clamp(v_t, 0.0, 1.0)), 1.0); }
"""

BONE_COLOR = (1.0, 0.75, 0.1)
JOINT_COLOR = (1.0, 0.95, 0.4)
ATTACH_COLOR = (0.2, 0.9, 1.0)
HIGHLIGHT_COLOR = (1.0, 0.2, 0.25)
GRID_MINOR = (0.19, 0.21, 0.26)
GRID_MAJOR = (0.27, 0.30, 0.37)
GRID_X = (0.62, 0.28, 0.30)  # the X and Y axes through the origin
GRID_Y = (0.30, 0.56, 0.36)


def _nice_step(raw: float) -> float:
    """1, 2 or 5 times a power of ten, at least ``raw``."""
    if raw <= 0:
        return 1.0
    power = 10.0 ** math.floor(math.log10(raw))
    for factor in (1.0, 2.0, 5.0, 10.0):
        if raw <= factor * power:
            return factor * power
    return 10.0 * power


def grid_lines(lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Floor grid under a model's bounds: ``(N, 6)`` line vertices (xyz +
    rgb) on the plane z = lo.z, every fifth line major, axes coloured."""
    radius = float(np.linalg.norm(hi - lo)) / 2 or 10.0
    step = _nice_step(radius / 4)
    half = math.ceil(radius * 1.6 / step) * step
    cx = round(float(lo[0] + hi[0]) / 2 / step) * step
    cy = round(float(lo[1] + hi[1]) / 2 / step) * step
    z = float(lo[2])
    out: list[list[float]] = []
    count = int(round(half / step))
    for i in range(-count, count + 1):
        for axis in (0, 1):
            value = (cx if axis == 0 else cy) + i * step
            if abs(value) < step * 1e-6:
                colour = GRID_Y if axis == 0 else GRID_X  # x = 0 is the Y axis
            else:
                colour = GRID_MAJOR if round(value / step) % 5 == 0 else GRID_MINOR
            if axis == 0:
                a, b = (value, cy - half, z), (value, cy + half, z)
            else:
                a, b = (cx - half, value, z), (cx + half, value, z)
            out.append([*a, *colour])
            out.append([*b, *colour])
    return np.array(out, dtype=np.float32)


FP_FOV = 74.0  # the game's view-model FOV (vertical)
FP_FOV_BY_KIND = {"zhands": 84.0}  # defaults per asset kind


@dataclass
class Camera:
    """Orbit camera around ``target`` (Z up) or a fixed first-person eye."""

    target: tuple[float, float, float] = (0.0, 0.0, 0.0)
    distance: float = 60.0
    yaw: float = -55.0  # degrees around Z; the eye sits in front-right of -Y
    pitch: float = 18.0
    fov: float = 60.0  # vertical, orbit camera
    # first person: vertical FOV as the game draws a view model — CS 1.6's
    # 90° horizontal at 4:3 is 73.7° vertical (HLAM shows 74 too); zombie
    # hands are often tuned for 84
    fp_fov: float = FP_FOV
    first_person: bool = False

    def eye(self) -> QVector3D:
        if self.first_person:
            return QVector3D(0.0, 0.0, 0.0)
        y, p = math.radians(self.yaw), math.radians(self.pitch)
        tx, ty, tz = self.target
        return QVector3D(tx + self.distance * math.cos(p) * math.cos(y),
                         ty + self.distance * math.cos(p) * math.sin(y),
                         tz + self.distance * math.sin(p))

    def view(self) -> QMatrix4x4:
        m = QMatrix4x4()
        if self.first_person:
            # a view model faces -Y in SMD space (+X in game)
            m.lookAt(QVector3D(0, 0, 0), QVector3D(0, -1, 0), QVector3D(0, 0, 1))
        else:
            m.lookAt(self.eye(), QVector3D(*self.target), QVector3D(0, 0, 1))
        return m

    def projection(self, aspect: float) -> QMatrix4x4:
        m = QMatrix4x4()
        near = 0.5 if self.first_person else max(self.distance * 0.01, 0.05)
        far = 4000.0 if self.first_person else max(self.distance * 20.0, 100.0)
        fov = self.fp_fov if self.first_person else self.fov
        m.perspective(fov, max(aspect, 1e-3), near, far)
        return m

    def frame(self, lo: np.ndarray, hi: np.ndarray) -> None:
        centre = (lo + hi) / 2
        radius = float(np.linalg.norm(hi - lo)) / 2 or 10.0
        self.target = tuple(float(c) for c in centre)
        self.distance = radius / math.tan(math.radians(self.fov / 2)) * 0.95
        self.first_person = False


@dataclass
class ViewState:
    sequence: int | None = 0
    frame: float = 0.0
    bodygroups: dict[str, int] = field(default_factory=dict)
    camera: Camera = field(default_factory=Camera)
    show_bones: bool = False
    show_attachments: bool = True
    wireframe: bool = False
    textured: bool = True
    highlight_bone: int | None = None  # drawn on top even with bones hidden
    show_grid: bool = True  # floor grid under the model (orbit camera only)
    mirror_x: bool = False  # right-handed view model (the game's cl_righthand 1)
    background_top: tuple[float, float, float] = (0.17, 0.19, 0.235)
    background_bottom: tuple[float, float, float] = (0.075, 0.082, 0.10)


class _GpuBatch:
    def __init__(self, count: int) -> None:
        self.vao = QOpenGLVertexArrayObject()
        self.vao.create()
        self.vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self.vbo.create()
        self.vbo.setUsagePattern(QOpenGLBuffer.UsagePattern.DynamicDraw)
        self.vbo.bind()
        self.vbo.allocate(count * 8 * 4)
        self.count = count


class Renderer:
    """Owns the GL objects of one context; call everything with it current."""

    def __init__(self) -> None:
        self.scene: ModelScene | None = None
        self._ready = False
        self._batches: list[_GpuBatch] = []
        self._textures: dict[str, QOpenGLTexture] = {}
        self._lines: _GpuBatch | None = None
        self._grid: tuple[int, np.ndarray] | None = None  # (id(scene), lines)
        self.warnings: list[str] = []  # texture problems of the last upload
        self.gl_info = ""  # vendor / renderer / version, for the log
        self._white: QOpenGLTexture | None = None

    # -- setup -------------------------------------------------------------
    def initialize(self) -> None:
        self.gl = QOpenGLContext.currentContext().functions()
        try:
            self.gl_info = " · ".join(
                str(self.gl.glGetString(code) or "?") for code in (0x1F00, 0x1F01, 0x1F02))
        except Exception:  # noqa: BLE001 - informational only
            self.gl_info = ""
        self.mesh_program = self._program(_MESH_VS, _MESH_FS)
        self.line_program = self._program(_LINE_VS, _LINE_FS)
        self.bg_program = self._program(_BG_VS, _BG_FS)
        self._bg_vao = QOpenGLVertexArrayObject()
        self._bg_vao.create()
        self._polygon_mode = None
        try:  # wireframe needs desktop GL; optional
            from PySide6.QtOpenGL import QOpenGLFunctions_3_3_Core, QOpenGLVersionFunctionsFactory
            core = QOpenGLVersionFunctionsFactory.get(None, QOpenGLContext.currentContext())
            if isinstance(core, QOpenGLFunctions_3_3_Core) or hasattr(core, "glPolygonMode"):
                self._polygon_mode = core.glPolygonMode
        except Exception:  # noqa: BLE001 - wireframe simply stays unavailable
            self._polygon_mode = None
        self._ready = True
        if self.scene is not None:
            self._upload(self.scene)

    @staticmethod
    def _program(vs: str, fs: str) -> QOpenGLShaderProgram:
        program = QOpenGLShaderProgram()
        if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex, vs) or \
                not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Fragment, fs) or \
                not program.link():
            raise RuntimeError(f"shader build failed: {program.log()}")
        return program

    def set_scene(self, scene: ModelScene | None) -> None:
        self.scene = scene
        if self._ready:
            self._release()
            if scene is not None:
                self._upload(scene)

    def shutdown(self) -> None:
        """Free every GL object (call with the context current, before it dies)."""
        self._release()
        for gpu in (self._lines,):
            if gpu is not None:
                gpu.vbo.destroy()
                gpu.vao.destroy()
        if self._white is not None:
            self._white.destroy()
        if getattr(self, "_bg_vao", None) is not None:
            self._bg_vao.destroy()
            self._bg_vao = None
        self._lines = self._white = None
        self._grid = None
        self._ready = False

    def _release(self) -> None:
        for gpu in self._batches:
            gpu.vbo.destroy()
            gpu.vao.destroy()
        for texture in self._textures.values():
            texture.destroy()
        self._batches, self._textures = [], {}

    def _upload(self, scene: ModelScene) -> None:
        self.warnings = []
        for batch in scene.batches:
            self._batches.append(_GpuBatch(len(batch.bones)))
        for material, path in scene.textures.items():
            if path is None:
                continue
            try:
                masked = scene.render_modes.get(material.lower()) == "masked"
                width, height, rgba = texture_rgba(path, masked=masked)
            except Exception as exc:  # noqa: BLE001 - an unreadable BMP draws untextured
                self.warnings.append(f"texture {material}: cannot read {path.name} ({exc})")
                continue
            image = QImage(rgba, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()
            texture = QOpenGLTexture(QOpenGLTexture.Target.Target2D)
            # rows go up top-first, so t = 0 is the image's top: SMD v = 1 - t
            texture.setData(image, QOpenGLTexture.MipMapGeneration.GenerateMipMaps)
            texture.setMinMagFilters(QOpenGLTexture.Filter.LinearMipMapLinear,
                                     QOpenGLTexture.Filter.Linear)
            texture.setWrapMode(QOpenGLTexture.WrapMode.Repeat)
            if not texture.isCreated() or texture.textureId() == 0:
                self.warnings.append(f"texture {material}: the GL texture was not created")
                continue
            self._textures[material] = texture
        missing = [m for m, path in scene.textures.items() if path is None]
        if missing:
            self.warnings.append(f"no texture file for {', '.join(missing[:6])}")
        if self._lines is None:
            self._lines = _GpuBatch(4096)
        if self._white is None:
            # bound when drawing untextured, so the sampler never reads an
            # empty unit (drivers warn and some draw black)
            white = QImage(1, 1, QImage.Format.Format_RGBA8888)
            white.fill(0xFFFFFFFF)
            self._white = QOpenGLTexture(white)

    # -- drawing -----------------------------------------------------------
    def render(self, width: int, height: int, state: ViewState) -> None:
        gl = self.gl
        # Qt composites widgets in this same context and may leave state
        # behind (a disabled depth mask makes glClear skip the depth buffer:
        # every later frame then depth-tests against garbage). Set it all.
        gl.glDepthMask(True)
        gl.glDepthFunc(GL_LESS)
        gl.glEnable(GL_DEPTH_TEST)
        gl.glDisable(GL_BLEND)
        gl.glDisable(GL_SCISSOR_TEST)
        gl.glColorMask(True, True, True, True)
        gl.glViewport(0, 0, width, height)
        gl.glClearColor(*state.background_bottom, 1.0)
        gl.glClearDepthf(1.0)
        gl.glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        if not self._ready:
            return
        self._draw_background(state)
        scene = self.scene
        if scene is None:
            return
        mvp = state.camera.projection(width / max(height, 1)) * state.camera.view()
        if state.mirror_x:  # cl_righthand 1: the game mirrors view models
            mirror = QMatrix4x4()
            mirror.scale(-1.0, 1.0, 1.0)
            mvp = mvp * mirror
        rot, trans = scene.world(*scene.local_pose(
            state.sequence if scene.sequences else None, state.frame))

        visible = {id(b) for b in scene.visible_batches(state.bodygroups)}
        program = self.mesh_program
        program.bind()
        program.setUniformValue("u_mvp", mvp)
        self._set_int(program, "u_tex", 0)
        # mirrored with the model, so a mirrored model is lit like the original
        light = QVector3D(-0.35 if state.mirror_x else 0.35, -0.6, 0.72).normalized()
        program.setUniformValue("u_light", light)
        gl.glEnable(GL_DEPTH_TEST)
        gl.glDisable(GL_CULL_FACE)
        if state.wireframe and self._polygon_mode is not None:
            self._polygon_mode(GL_FRONT_AND_BACK, GL_LINE)
        for additive_pass in (False, True):
            if additive_pass:
                gl.glEnable(GL_BLEND)
                gl.glBlendFunc(GL_ONE, GL_ONE)
                gl.glDepthMask(False)
            for batch, gpu in zip(scene.batches, self._batches, strict=True):
                if id(batch) not in visible or (batch.render_mode == "additive") != additive_pass:
                    continue
                pos, nrm = scene.skin(batch, rot, trans)
                data = np.hstack([pos, nrm, np.column_stack(
                    [batch.uv[:, 0], 1.0 - batch.uv[:, 1]])]).astype(np.float32)
                texture = self._textures.get(batch.material) if state.textured else None
                self._set_int(program, "u_textured", 1 if texture is not None else 0)
                self._set_int(program, "u_mode",
                              {"masked": 1, "additive": 2}.get(batch.render_mode, 0))
                bound = texture or self._white
                bound.bind(0)
                self._draw(program, gpu, data, GL_TRIANGLES, (3, 3, 2))
                bound.release(0)
            if additive_pass:
                gl.glDepthMask(True)
                gl.glDisable(GL_BLEND)
        if state.wireframe and self._polygon_mode is not None:
            self._polygon_mode(GL_FRONT_AND_BACK, GL_FILL)
        program.release()

        # The floor grid comes AFTER the model, depth-tested (the model hides
        # it) without depth writes — the same path as the bone/attachment
        # lines. Drawn before the model it made the model vanish on a Windows
        # driver (lines first, then the mesh VAOs, drew nothing).
        if state.show_grid and not state.camera.first_person and self._lines is not None:
            if self._grid is None or self._grid[0] != id(scene):
                self._grid = (id(scene), grid_lines(*scene.bounds()))
            gl.glEnable(GL_DEPTH_TEST)
            gl.glDepthMask(False)
            line = self.line_program
            line.bind()
            line.setUniformValue("u_mvp", mvp)
            line.setUniformValue("u_point", 1.0)
            self._draw(line, self._lines, self._grid[1], GL_LINES, (3, 3))
            line.release()
            gl.glDepthMask(True)

        # Markers are 3D crosses made of lines: GL_POINTS with a shader point
        # size draws nothing on some core-profile drivers (macOS).
        cam = state.camera
        size = (0.5 if cam.first_person else max(cam.distance, 1.0)) * 0.012

        def crosses(points: np.ndarray, colour: tuple[float, float, float],
                    scale: float) -> np.ndarray:
            out = []
            for axis in np.eye(3) * size * scale:
                out.append(np.hstack([points - axis, np.tile(colour, (len(points), 1))]))
                out.append(np.hstack([points + axis, np.tile(colour, (len(points), 1))]))
            pairs = np.stack(out).reshape(3, 2, len(points), 6)
            return pairs.transpose(0, 2, 1, 3).reshape(-1, 6)

        overlay: list[np.ndarray] = []
        if state.show_bones:
            for b, parent in enumerate(scene.parents):
                if parent >= 0:
                    overlay.append(np.array([[*trans[parent], *BONE_COLOR],
                                             [*trans[b], *BONE_COLOR]]))
            overlay.append(crosses(trans, JOINT_COLOR, 0.6))
        if state.show_attachments and scene.attachments:
            pts = np.array([rot[bone] @ offset + trans[bone]
                            for _i, bone, offset in scene.attachments])
            overlay.append(crosses(pts, ATTACH_COLOR, 1.4))
        hb = state.highlight_bone
        if hb is not None and 0 <= hb < len(trans):
            parent = scene.parents[hb]
            if parent >= 0:
                overlay.append(np.array([[*trans[parent], *HIGHLIGHT_COLOR],
                                         [*trans[hb], *HIGHLIGHT_COLOR]]))
            overlay.append(crosses(trans[hb:hb + 1], HIGHLIGHT_COLOR, 2.0))
        if overlay:
            gl.glDisable(GL_DEPTH_TEST)
            line = self.line_program
            line.bind()
            line.setUniformValue("u_mvp", mvp)
            line.setUniformValue("u_point", 1.0)
            self._draw(line, self._lines, np.concatenate(overlay).astype(np.float32),
                       GL_LINES, (3, 3))
            line.release()
            gl.glEnable(GL_DEPTH_TEST)

    def _set_int(self, program: QOpenGLShaderProgram, name: str, value: int) -> None:
        """An int/sampler uniform, always through glUniform1i. PySide may
        resolve ``setUniformValue(name, True / 0)`` to its float overload
        (glUniform1f): macOS accepts that, some Windows drivers reject it
        (GL_INVALID_OPERATION) — the flag then stays 0 and every model draws
        untextured grey."""
        self.gl.glUniform1i(program.uniformLocation(name), int(value))

    def _draw_background(self, state: ViewState) -> None:
        gl = self.gl
        gl.glDisable(GL_DEPTH_TEST)
        gl.glDepthMask(False)
        program = self.bg_program
        program.bind()
        program.setUniformValue("u_top", QVector3D(*state.background_top))
        program.setUniformValue("u_bottom", QVector3D(*state.background_bottom))
        self._bg_vao.bind()
        gl.glDrawArrays(GL_TRIANGLES, 0, 3)
        self._bg_vao.release()
        program.release()
        gl.glDepthMask(True)
        gl.glEnable(GL_DEPTH_TEST)

    def _draw(self, program: QOpenGLShaderProgram, gpu: _GpuBatch, data: np.ndarray,
              mode: int, layout: tuple[int, ...]) -> None:
        count = len(data)
        if count == 0:
            return
        stride = sum(layout) * 4
        gpu.vao.bind()
        gpu.vbo.bind()
        if count > gpu.count:
            gpu.vbo.allocate(count * stride)
            gpu.count = count
        gpu.vbo.write(0, data.tobytes(), data.nbytes)
        offset = 0
        for location, size in enumerate(layout):
            program.enableAttributeArray(location)
            program.setAttributeBuffer(location, GL_FLOAT, offset, size, stride)
            offset += size * 4
        self.gl.glDrawArrays(mode, 0, count)
        gpu.vao.release()


def render_offscreen(scene: ModelScene, state: ViewState, width: int = 640,
                     height: int = 480) -> QImage:
    """Render ``scene`` into an image with a private GL context (needs a
    platform with OpenGL: not Qt's ``offscreen`` plugin)."""
    from PySide6.QtGui import QOffscreenSurface
    from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat

    context = QOpenGLContext()
    context.setFormat(gl_format())
    if not context.create():
        raise RuntimeError("no OpenGL context")
    surface = QOffscreenSurface()
    surface.setFormat(context.format())
    surface.create()
    context.makeCurrent(surface)
    fbo_format = QOpenGLFramebufferObjectFormat()
    fbo_format.setAttachment(QOpenGLFramebufferObject.Attachment.Depth)
    fbo_format.setSamples(4)
    fbo = QOpenGLFramebufferObject(width, height, fbo_format)
    fbo.bind()
    renderer = Renderer()
    renderer.initialize()
    renderer.set_scene(scene)
    renderer.render(width, height, state)
    image = fbo.toImage()
    renderer.shutdown()
    fbo.release()
    context.doneCurrent()
    return image


def gl_format():
    from PySide6.QtGui import QSurfaceFormat
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.OpenGLContextProfile.CoreProfile)
    fmt.setDepthBufferSize(24)
    fmt.setSamples(4)
    return fmt


__all__ = ["Camera", "Renderer", "ViewState", "gl_format", "render_offscreen"]
