import time
import numpy as np
import pyray as rl
from cereal import log, messaging
from msgq.visionipc import VisionStreamType
from openpilot.selfdrive.ui import UI_BORDER_SIZE
from openpilot.selfdrive.ui.ui_state import ui_state, UIStatus
from openpilot.selfdrive.ui.onroad.alert_renderer import AlertRenderer
from openpilot.selfdrive.ui.onroad.driver_state import DriverStateRenderer
from openpilot.selfdrive.ui.onroad.hud_renderer import HudRenderer
from openpilot.selfdrive.ui.onroad.model_renderer import ModelRenderer
from openpilot.selfdrive.ui.onroad.cameraview import CameraView
from openpilot.system.ui.lib.application import gui_app, FontWeight
from openpilot.common.transformations.camera import DEVICE_CAMERAS, DeviceCameraConfig, view_frame_from_device_frame
from openpilot.common.transformations.orientation import rot_from_euler

if gui_app.sunnypilot_ui():
  from openpilot.selfdrive.ui.sunnypilot.onroad.augmented_road_view import BORDER_COLORS_SP, AugmentedRoadViewSP
  from openpilot.selfdrive.ui.sunnypilot.onroad.driver_state import DriverStateRendererSP as DriverStateRenderer
  from openpilot.selfdrive.ui.sunnypilot.onroad.hud_renderer import HudRendererSP as HudRenderer
  from openpilot.selfdrive.ui.sunnypilot.ui_state import OnroadTimerStatus

OpState = log.SelfdriveState.OpenpilotState
CALIBRATED = log.LiveCalibrationData.Status.calibrated
ROAD_CAM = VisionStreamType.VISION_STREAM_ROAD
WIDE_CAM = VisionStreamType.VISION_STREAM_WIDE_ROAD
DEFAULT_DEVICE_CAMERA = DEVICE_CAMERAS["tici", "ar0231"]

BORDER_COLORS = {
  UIStatus.DISENGAGED: rl.Color(0x12, 0x28, 0x39, 0xFF),  # Blue for disengaged state
  UIStatus.OVERRIDE: rl.Color(0x89, 0x92, 0x8D, 0xFF),  # Gray for override state
  UIStatus.ENGAGED: rl.Color(0x16, 0x7F, 0x40, 0xFF),  # Green for engaged state
  **BORDER_COLORS_SP,
}

WIDE_CAM_MAX_SPEED = 10.0  # m/s (22 mph)
ROAD_CAM_MIN_SPEED = 15.0  # m/s (34 mph)
INF_POINT = np.array([1000.0, 0.0, 0.0])


class AugmentedRoadView(CameraView, AugmentedRoadViewSP):
  def __init__(self, stream_type: VisionStreamType = VisionStreamType.VISION_STREAM_ROAD):
    CameraView.__init__(self, "camerad", stream_type)
    AugmentedRoadViewSP.__init__(self)
    self._set_placeholder_color(BORDER_COLORS[UIStatus.DISENGAGED])

    self.device_camera: DeviceCameraConfig | None = None
    self.view_from_calib = view_frame_from_device_frame.copy()
    self.view_from_wide_calib = view_frame_from_device_frame.copy()

    self._matrix_cache_key = (0, 0.0, 0.0, stream_type)
    self._cached_matrix: np.ndarray | None = None
    self._content_rect = rl.Rectangle()

    self.model_renderer = ModelRenderer()
    self._hud_renderer = HudRenderer()
    self.alert_renderer = AlertRenderer()
    self.driver_state_renderer = DriverStateRenderer()
    self._font_medium = gui_app.font(FontWeight.MEDIUM)
    self._font_bold = gui_app.font(FontWeight.BOLD)

    # debug
    self._pm = messaging.PubMaster(['uiDebug'])

  def _render(self, rect):
    # Only render when system is started to avoid invalid data access
    start_draw = time.monotonic()
    if not ui_state.started:
      return

    self._switch_stream_if_needed(ui_state.sm)

    # Update calibration before rendering
    self._update_calibration()

    # Create inner content area with border padding
    self._content_rect = rl.Rectangle(
      rect.x + UI_BORDER_SIZE,
      rect.y + UI_BORDER_SIZE,
      rect.width - 2 * UI_BORDER_SIZE,
      rect.height - 2 * UI_BORDER_SIZE,
    )

    # Enable scissor mode to clip all rendering within content rectangle boundaries
    # This creates a rendering viewport that prevents graphics from drawing outside the border
    rl.begin_scissor_mode(
      int(self._content_rect.x),
      int(self._content_rect.y),
      int(self._content_rect.width),
      int(self._content_rect.height)
    )

    # Render the base camera view
    super()._render(rect)

    # Draw all UI overlays
    self.model_renderer.render(self._content_rect)
    AugmentedRoadViewSP.update_fade_out_bottom_overlay(self, self._content_rect)
    self._hud_renderer.render(self._content_rect)
    self.alert_renderer.render(self._content_rect)
    self.driver_state_renderer.render(self._content_rect)
    self._draw_llm_advisory(self._content_rect)

    # Custom UI extension point - add custom overlays here
    # Use self._content_rect for positioning within camera bounds

    # End clipping region
    rl.end_scissor_mode()

    # Draw colored border based on driving state
    self._draw_border(rect)

    # publish uiDebug
    msg = messaging.new_message('uiDebug')
    msg.uiDebug.drawTimeMillis = (time.monotonic() - start_draw) * 1000
    self._pm.send('uiDebug', msg)

  def _draw_llm_advisory(self, rect: rl.Rectangle) -> None:
    if not ui_state.started:
      return
    if not ui_state.params.get_bool("LLMAgentEnabled"):
      return
    advisory = ui_state.llm_advisory
    if not advisory:
      return

    rows = self._llm_inspection_rows(advisory)
    if not rows:
      return

    panel_w = min(840, max(500, int(rect.width * 0.60)))
    panel_w = min(panel_w, int(rect.width - 72))
    if panel_w <= 0:
      return

    fields = {label: value for label, value in rows}
    header_size = 25
    category_size = 40
    label_size = 21
    info_size = 29
    detail_size = 26
    text_gap = 5
    inner_pad_x = 42
    inner_w = max(0, int(panel_w - inner_pad_x * 2))
    category = self._fit_llm_text(self._font_bold, fields.get("CATEGORY", "Road asset issue").upper(),
                                  category_size, inner_w)
    location_lines = self._wrap_llm_text(self._font_bold, fields.get("LOCATION", "roadway"), info_size, inner_w, 1)
    detail_lines = self._wrap_llm_text(self._font_bold, fields.get("DETAIL", "field verification recommended"),
                                       detail_size, inner_w, 2)

    panel_h = (22 + header_size + 8 + category_size + 14 + label_size + 4 +
               len(location_lines) * (info_size + text_gap) + 10 + label_size + 4 +
               len(detail_lines) * (detail_size + text_gap) + 18)
    x = rect.x + (rect.width - panel_w) / 2
    y = rect.y + (rect.height - panel_h) * 0.42
    panel = rl.Rectangle(x, y, panel_w, panel_h)

    bg = rl.Color(6, 12, 16, 132)
    border = rl.Color(255, 186, 55, 205)
    accent = rl.Color(255, 205, 68, 235)
    label_color = rl.Color(200, 222, 224, 225)
    value_color = rl.Color(255, 255, 245, 245)
    detail_color = rl.Color(232, 240, 244, 235)

    rl.draw_rectangle_rounded(rl.Rectangle(x + 4, y + 4, panel_w, panel_h), 0.08, 8, rl.Color(0, 0, 0, 62))
    rl.draw_rectangle_rounded(panel, 0.08, 8, bg)
    rl.draw_rectangle_rounded_lines_ex(panel, 0.08, 8, 3, border)
    rl.draw_rectangle(int(x + 14), int(y + 16), 7, int(panel_h - 32), accent)

    inner_x = x + inner_pad_x
    cursor_y = y + 18
    self._draw_shadowed_llm_text(self._font_medium, "MAINTENANCE FINDING",
                                 rl.Vector2(inner_x, cursor_y), header_size, accent)
    cursor_y += header_size + 8
    self._draw_shadowed_llm_text(self._font_bold, category, rl.Vector2(inner_x, cursor_y), category_size, value_color)
    cursor_y += category_size + 14

    self._draw_shadowed_llm_text(self._font_medium, "LOCATION", rl.Vector2(inner_x, cursor_y), label_size, label_color)
    cursor_y += label_size + 4
    for line in location_lines:
      self._draw_shadowed_llm_text(self._font_bold, line, rl.Vector2(inner_x, cursor_y), info_size, value_color)
      cursor_y += info_size + text_gap

    cursor_y += 5
    self._draw_shadowed_llm_text(self._font_medium, "DETAIL", rl.Vector2(inner_x, cursor_y), label_size, label_color)
    cursor_y += label_size + 4
    for line in detail_lines:
      self._draw_shadowed_llm_text(self._font_bold, line, rl.Vector2(inner_x, cursor_y), detail_size, detail_color)
      cursor_y += detail_size + text_gap

  def _llm_inspection_rows(self, advisory: str) -> list[tuple[str, str]]:
    fields = {}
    for part in advisory.split(";"):
      if "=" not in part:
        continue
      key, value = part.split("=", 1)
      key = key.strip().upper()
      value = " ".join(value.strip().split())
      if key and value:
        fields[key] = value

    if not fields:
      category = " ".join(advisory.strip().split())[:56]
      fields = {
        "CATEGORY": category,
        "LOCATION": "roadway",
        "DETAIL": "field verification recommended",
      }

    rows = [
      ("CATEGORY", fields.get("CATEGORY", "Road asset issue")),
      ("LOCATION", fields.get("LOCATION", "roadway")),
      ("DETAIL", fields.get("DETAIL", fields.get("ACTION", "field verification recommended"))),
    ]
    return [(label, value) for label, value in rows if value]

  def _fit_llm_text(self, font: rl.Font, text: str, font_size: int, max_width: int) -> str:
    text = " ".join(text.replace("\n", " ").split())
    if max_width <= 0:
      return ""
    if rl.measure_text_ex(font, text, font_size, 0).x <= max_width:
      return text
    while len(text) > 3 and rl.measure_text_ex(font, text + "...", font_size, 0).x > max_width:
      text = text[:-1].rstrip()
    return f"{text}..." if text else ""

  def _wrap_llm_text(self, font: rl.Font, text: str, font_size: int, max_width: int, max_lines: int) -> list[str]:
    text = " ".join(text.replace("\n", " ").split())
    if max_width <= 0 or not text:
      return [""]

    words = text.split()
    lines = []
    while words and len(lines) < max_lines:
      line = ""
      while words:
        candidate = words[0] if not line else f"{line} {words[0]}"
        if rl.measure_text_ex(font, candidate, font_size, 0).x <= max_width:
          line = candidate
          words.pop(0)
        else:
          break

      if not line:
        line = self._fit_llm_text(font, words.pop(0), font_size, max_width)
      lines.append(line)

    if words and lines:
      lines[-1] = self._fit_llm_text(font, f"{lines[-1]}...", font_size, max_width)
    return lines or [""]

  def _draw_shadowed_llm_text(self, font: rl.Font, text: str, pos: rl.Vector2, font_size: int, color: rl.Color) -> None:
    if not text:
      return
    shadow = rl.Color(0, 0, 0, 205)
    rl.draw_text_ex(font, text, rl.Vector2(pos.x + 2, pos.y + 2), font_size, 0, shadow)
    rl.draw_text_ex(font, text, pos, font_size, 0, color)

  def _handle_mouse_press(self, _):
    if not self._hud_renderer.user_interacting() and self._click_callback is not None:
      self._click_callback()

  def _handle_mouse_release(self, _):
    # We only call click callback on press if not interacting with HUD
    pass

  def _draw_border(self, rect: rl.Rectangle):
    rl.draw_rectangle_lines_ex(rect, UI_BORDER_SIZE, rl.BLACK)
    border_roundness = 0.12
    border_color = BORDER_COLORS.get(ui_state.status, BORDER_COLORS[UIStatus.DISENGAGED])
    border_rect = rl.Rectangle(rect.x + UI_BORDER_SIZE, rect.y + UI_BORDER_SIZE,
                               rect.width - 2 * UI_BORDER_SIZE, rect.height - 2 * UI_BORDER_SIZE)
    rl.draw_rectangle_rounded_lines_ex(border_rect, border_roundness, 10, UI_BORDER_SIZE, border_color)

  def _switch_stream_if_needed(self, sm):
    if sm['selfdriveState'].experimentalMode and WIDE_CAM in self.available_streams:
      v_ego = sm['carState'].vEgo
      if v_ego < WIDE_CAM_MAX_SPEED:
        target = WIDE_CAM
      elif v_ego > ROAD_CAM_MIN_SPEED:
        target = ROAD_CAM
      else:
        # Hysteresis zone - keep current stream
        target = self.stream_type
    else:
      target = ROAD_CAM

    if self.stream_type != target:
      self.switch_stream(target)

  def _update_calibration(self):
    # Update device camera if not already set
    sm = ui_state.sm
    if not self.device_camera and sm.seen['roadCameraState'] and sm.seen['deviceState']:
      self.device_camera = DEVICE_CAMERAS[(str(sm['deviceState'].deviceType), str(sm['roadCameraState'].sensor))]

    # Check if live calibration data is available and valid
    if not (sm.updated["liveCalibration"] and sm.valid['liveCalibration']):
      return

    calib = sm['liveCalibration']
    if len(calib.rpyCalib) != 3 or calib.calStatus != CALIBRATED:
      return

    # Update view_from_calib matrix
    device_from_calib = rot_from_euler(calib.rpyCalib)
    self.view_from_calib = view_frame_from_device_frame @ device_from_calib

    # Update wide calibration if available
    if hasattr(calib, 'wideFromDeviceEuler') and len(calib.wideFromDeviceEuler) == 3:
      wide_from_device = rot_from_euler(calib.wideFromDeviceEuler)
      self.view_from_wide_calib = view_frame_from_device_frame @ wide_from_device @ device_from_calib

  def _calc_frame_matrix(self, rect: rl.Rectangle) -> np.ndarray:
    # Check if we can use cached matrix
    cache_key = (
      ui_state.sm.recv_frame['liveCalibration'],
      self._content_rect.width,
      self._content_rect.height,
      self.stream_type
    )
    if cache_key == self._matrix_cache_key and self._cached_matrix is not None:
      return self._cached_matrix

    # Get camera configuration
    device_camera = self.device_camera or DEFAULT_DEVICE_CAMERA
    is_wide_camera = self.stream_type == WIDE_CAM
    intrinsic = device_camera.ecam.intrinsics if is_wide_camera else device_camera.fcam.intrinsics
    calibration = self.view_from_wide_calib if is_wide_camera else self.view_from_calib
    zoom = 2.0 if is_wide_camera else 1.1

    # Calculate transforms for vanishing point
    calib_transform = intrinsic @ calibration
    kep = calib_transform @ INF_POINT

    # Calculate center points and dimensions
    x, y = self._content_rect.x, self._content_rect.y
    w, h = self._content_rect.width, self._content_rect.height
    cx, cy = intrinsic[0, 2], intrinsic[1, 2]

    # Calculate max allowed offsets with margins
    margin = 5
    max_x_offset = cx * zoom - w / 2 - margin
    max_y_offset = cy * zoom - h / 2 - margin

    # Calculate and clamp offsets to prevent out-of-bounds issues
    try:
      if abs(kep[2]) > 1e-6:
        x_offset = np.clip((kep[0] / kep[2] - cx) * zoom, -max_x_offset, max_x_offset)
        y_offset = np.clip((kep[1] / kep[2] - cy) * zoom, -max_y_offset, max_y_offset)
      else:
        x_offset, y_offset = 0, 0
    except (ZeroDivisionError, OverflowError):
      x_offset, y_offset = 0, 0

    # Cache the computed transformation matrix to avoid recalculations
    self._matrix_cache_key = cache_key
    self._cached_matrix = np.array([
      [zoom * 2 * cx / w, 0, -x_offset / w * 2],
      [0, zoom * 2 * cy / h, -y_offset / h * 2],
      [0, 0, 1.0]
    ])

    video_transform = np.array([
      [zoom, 0.0, (w / 2 + x - x_offset) - (cx * zoom)],
      [0.0, zoom, (h / 2 + y - y_offset) - (cy * zoom)],
      [0.0, 0.0, 1.0]
    ])
    self.model_renderer.set_transform(video_transform @ calib_transform)

    return self._cached_matrix

  def show_event(self):
    if gui_app.sunnypilot_ui():
      ui_state.reset_onroad_sleep_timer(OnroadTimerStatus.RESUME)

  def hide_event(self):
    if gui_app.sunnypilot_ui():
      ui_state.reset_onroad_sleep_timer(OnroadTimerStatus.PAUSE)


if __name__ == "__main__":
  gui_app.init_window("OnRoad Camera View")
  road_camera_view = AugmentedRoadView(ROAD_CAM)
  print("***press space to switch camera view***")
  try:
    for _ in gui_app.render():
      ui_state.update()
      if rl.is_key_released(rl.KeyboardKey.KEY_SPACE):
        if WIDE_CAM in road_camera_view.available_streams:
          stream = ROAD_CAM if road_camera_view.stream_type == WIDE_CAM else WIDE_CAM
          road_camera_view.switch_stream(stream)
      road_camera_view.render(rl.Rectangle(0, 0, gui_app.width, gui_app.height))
  finally:
    road_camera_view.close()
