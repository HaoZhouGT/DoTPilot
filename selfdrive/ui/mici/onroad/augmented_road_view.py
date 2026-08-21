import time
import numpy as np
import pyray as rl
from cereal import messaging, car, log
from msgq.visionipc import VisionStreamType
from openpilot.selfdrive.ui.ui_state import ui_state, UIStatus
from openpilot.selfdrive.ui.mici.onroad import SIDE_PANEL_WIDTH
from openpilot.selfdrive.ui.mici.onroad.alert_renderer import AlertRenderer
from openpilot.selfdrive.ui.mici.onroad.driver_state import DriverStateRenderer
from openpilot.selfdrive.ui.mici.onroad.hud_renderer import HudRenderer
from openpilot.selfdrive.ui.mici.onroad.model_renderer import ModelRenderer
from openpilot.selfdrive.ui.mici.onroad.confidence_ball import ConfidenceBall
from openpilot.selfdrive.ui.mici.onroad.cameraview import CameraView
from openpilot.system.ui.lib.application import FontWeight, gui_app, MousePos, MouseEvent
from openpilot.system.ui.widgets.label import UnifiedLabel
from openpilot.system.ui.widgets import Widget
from openpilot.common.filter_simple import BounceFilter, FirstOrderFilter
from openpilot.common.transformations.camera import DEVICE_CAMERAS, DeviceCameraConfig, view_frame_from_device_frame
from openpilot.common.transformations.orientation import rot_from_euler
from enum import IntEnum

if gui_app.sunnypilot_ui():
  from openpilot.selfdrive.ui.sunnypilot.mici.onroad.hud_renderer import HudRendererSP as HudRenderer
  from openpilot.selfdrive.ui.sunnypilot.ui_state import OnroadTimerStatus

OpState = log.SelfdriveState.OpenpilotState
CALIBRATED = log.LiveCalibrationData.Status.calibrated
ROAD_CAM = VisionStreamType.VISION_STREAM_ROAD
WIDE_CAM = VisionStreamType.VISION_STREAM_WIDE_ROAD
DEFAULT_DEVICE_CAMERA = DEVICE_CAMERAS["tici", "ar0231"]


class BookmarkState(IntEnum):
  HIDDEN = 0
  DRAGGING = 1
  TRIGGERED = 2

WIDE_CAM_MAX_SPEED = 5.0  # m/s (10 mph)
ROAD_CAM_MIN_SPEED = 10  # m/s (25 mph)

CAM_Y_OFFSET = 20


class BookmarkIcon(Widget):
  PEEK_THRESHOLD = 50  # If icon peeks out this much, snap it fully visible
  FULL_VISIBLE_OFFSET = 200  # How far onscreen when fully visible
  HIDDEN_OFFSET = -50  # How far offscreen when hidden

  def __init__(self, bookmark_callback):
    super().__init__()
    self._bookmark_callback = bookmark_callback
    self._icon = gui_app.texture("icons_mici/onroad/bookmark.png", 180, 180)
    self._icon_fill = gui_app.texture("icons_mici/onroad/bookmark_fill.png", 180, 180)
    self._active_icon = self._icon
    self._offset_filter = BounceFilter(0.0, 0.1, 1 / gui_app.target_fps)

    # State
    self._interacting = False
    self._state = BookmarkState.HIDDEN
    self._swipe_start_x = 0.0
    self._swipe_current_x = 0.0
    self._is_swiping = False
    self._is_swiping_left: bool = False
    self._triggered_time: float = 0.0

  def is_swiping_left(self) -> bool:
    """Check if currently swiping left (for scroller to disable)."""
    return self._is_swiping_left

  def interacting(self):
    interacting, self._interacting = self._interacting, False
    return interacting

  def _update_state(self):
    if self._state == BookmarkState.DRAGGING:
      # Allow pulling past activated position with rubber band effect
      swipe_offset = self._swipe_start_x - self._swipe_current_x
      swipe_offset = min(swipe_offset, self.FULL_VISIBLE_OFFSET + 50)
      self._offset_filter.update(swipe_offset)

    elif self._state == BookmarkState.TRIGGERED:
      # Continue animating to fully visible
      self._offset_filter.update(self.FULL_VISIBLE_OFFSET)
      # Stay in TRIGGERED state for 1 second
      if rl.get_time() - self._triggered_time >= 1.5:
        self._state = BookmarkState.HIDDEN

    elif self._state == BookmarkState.HIDDEN:
      self._offset_filter.update(self.HIDDEN_OFFSET)

      if self._offset_filter.x < 1e-3:
        self._interacting = False
        self._active_icon = self._icon

  def _handle_mouse_event(self, mouse_event: MouseEvent):
    if not ui_state.started:
      return

    if mouse_event.left_pressed:
      # Store relative position within widget
      self._swipe_start_x = mouse_event.pos.x
      self._swipe_current_x = mouse_event.pos.x
      self._is_swiping = True
      self._is_swiping_left = False
      self._state = BookmarkState.DRAGGING
      self._active_icon = self._icon

    elif mouse_event.left_down and self._is_swiping:
      self._swipe_current_x = mouse_event.pos.x
      swipe_offset = self._swipe_start_x - self._swipe_current_x
      self._is_swiping_left = swipe_offset > 0
      if self._is_swiping_left:
        self._interacting = True

    elif mouse_event.left_released:
      if self._is_swiping:
        swipe_distance = self._swipe_start_x - self._swipe_current_x

        # If peeking past threshold, transition to animating to fully visible and bookmark
        if swipe_distance > self.PEEK_THRESHOLD:
          self._state = BookmarkState.TRIGGERED
          self._triggered_time = rl.get_time()
          self._active_icon = self._icon_fill
          self._bookmark_callback()
        else:
          # Otherwise, transition back to hidden
          self._state = BookmarkState.HIDDEN

        # Reset swipe state
        self._is_swiping = False
        self._is_swiping_left = False

  def _render(self, _):
    """Render the bookmark icon."""
    if self._offset_filter.x > 0:
      icon_x = self.rect.x + self.rect.width - round(self._offset_filter.x)
      icon_y = self.rect.y + (self.rect.height - self._active_icon.height) / 2  # Vertically centered
      rl.draw_texture(self._active_icon, int(icon_x), int(icon_y), rl.WHITE)


class AugmentedRoadView(CameraView):
  def __init__(self, bookmark_callback=None, stream_type: VisionStreamType = VisionStreamType.VISION_STREAM_ROAD):
    super().__init__("camerad", stream_type)
    self._bookmark_callback = bookmark_callback
    self._set_placeholder_color(rl.BLACK)

    self.device_camera: DeviceCameraConfig | None = None
    self.view_from_calib = view_frame_from_device_frame.copy()
    self.view_from_wide_calib = view_frame_from_device_frame.copy()

    self._last_calib_time: float = 0
    self._last_rect_dims = (0.0, 0.0)
    self._last_stream_type = stream_type
    self._cached_matrix: np.ndarray | None = None
    self._content_rect = rl.Rectangle()
    self._last_click_time = 0.0

    # Bookmark icon with swipe gesture
    self._bookmark_icon = BookmarkIcon(bookmark_callback)

    self._model_renderer = ModelRenderer()
    self._hud_renderer = HudRenderer()
    self._alert_renderer = AlertRenderer()
    self._driver_state_renderer = DriverStateRenderer()
    self._confidence_ball = ConfidenceBall()
    self._offroad_label = UnifiedLabel("start the car to\nuse sunnypilot", 54, FontWeight.DISPLAY,
                                       text_color=rl.Color(255, 255, 255, int(255 * 0.9)),
                                       alignment=rl.GuiTextAlignment.TEXT_ALIGN_CENTER,
                                       alignment_vertical=rl.GuiTextAlignmentVertical.TEXT_ALIGN_MIDDLE)
    self._llm_advisory_font = gui_app.font(FontWeight.BOLD)
    self._llm_advisory_meta_font = gui_app.font(FontWeight.MEDIUM)

    self._fade_texture = gui_app.texture("icons_mici/onroad/onroad_fade.png")
    self._fade_alpha_filter = FirstOrderFilter(0, 0.1, 1 / gui_app.target_fps)

    # debug
    self._pm = messaging.PubMaster(['uiDebug'])

  def is_swiping_left(self) -> bool:
    """Check if currently swiping left (for scroller to disable)."""
    return self._bookmark_icon.is_swiping_left()

  def _update_state(self):
    super()._update_state()

    # update offroad label
    if ui_state.panda_type == log.PandaState.PandaType.unknown:
      self._offroad_label.set_text("system booting")
    else:
      self._offroad_label.set_text("start the car to\nuse sunnypilot")

  def _handle_mouse_release(self, mouse_pos: MousePos):
    # Don't trigger click callback if bookmark was triggered
    if not self._bookmark_icon.interacting():
      super()._handle_mouse_release(mouse_pos)

  def _render(self, _):
    start_draw = time.monotonic()
    self._switch_stream_if_needed(ui_state.sm)

    # Update calibration before rendering
    self._update_calibration()

    # Create inner content area with border padding
    self._content_rect = rl.Rectangle(
      self.rect.x,
      self.rect.y,
      self.rect.width - SIDE_PANEL_WIDTH,
      self.rect.height,
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
    super()._render(self._content_rect)

    # Draw all UI overlays
    self._model_renderer.render(self._content_rect)

    # Fade out bottom of overlays for looks (only when engaged)
    fade_alpha = self._fade_alpha_filter.update(ui_state.status != UIStatus.DISENGAGED)
    if fade_alpha > 1e-2:
      rl.draw_texture_ex(self._fade_texture, rl.Vector2(self._content_rect.x, self._content_rect.y), 0.0, 1.0,
                         rl.Color(255, 255, 255, int(255 * fade_alpha)))

    alert_to_render, not_animating_out = self._alert_renderer.will_render()

    # Hide DMoji when disengaged unless AlwaysOnDM is enabled
    should_draw_dmoji = (not self._hud_renderer.drawing_top_icons() and ui_state.is_onroad() and
                         (ui_state.status != UIStatus.DISENGAGED or ui_state.always_on_dm))
    self._driver_state_renderer.set_should_draw(should_draw_dmoji)
    self._driver_state_renderer.set_position(self._rect.x + 16, self._rect.y + 10)
    self._driver_state_renderer.render()

    self._hud_renderer.set_can_draw_top_icons(alert_to_render is None)
    self._hud_renderer.set_wheel_critical_icon(alert_to_render is not None and not not_animating_out and
                                               alert_to_render.visual_alert == car.CarControl.HUDControl.VisualAlert.steerRequired)
    # TODO: have alert renderer draw offroad mici label below
    if ui_state.started:
      self._alert_renderer.render(self._content_rect)
    self._hud_renderer.render(self._content_rect)
    self._draw_llm_advisory(self._content_rect)

    # Draw fake rounded border
    rl.draw_rectangle_rounded_lines_ex(self._content_rect, 0.2 * 1.02, 10, 50, rl.BLACK)

    # End clipping region
    rl.end_scissor_mode()

    # Custom UI extension point - add custom overlays here
    # Use self._content_rect for positioning within camera bounds
    self._confidence_ball.render(self.rect)

    self._bookmark_icon.render(self.rect)

    # Draw darkened background and text if not onroad
    if not ui_state.started:
      rl.draw_rectangle(int(self.rect.x), int(self.rect.y), int(self.rect.width), int(self.rect.height), rl.Color(0, 0, 0, 175))
      self._offroad_label.render(self._content_rect)

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

    panel_w = min(740, max(420, int(rect.width * 0.58)))
    panel_w = min(panel_w, int(rect.width - 64))
    if panel_w <= 0:
      return

    header_size = 28
    label_size = 21
    value_size = 34
    detail_size = 27
    row_h = 44
    label_col_w = min(164, panel_w * 0.30)
    inner_x = (rect.x + (rect.width - panel_w) / 2) + 34
    value_x = inner_x + label_col_w
    value_w = max(0, int(rect.x + (rect.width + panel_w) / 2 - value_x - 22))
    render_rows = []
    for label, value in rows:
      if label == "DETAIL":
        value_lines = self._wrap_llm_text(self._llm_advisory_font, value, detail_size, value_w, 2)
        row_height = row_h + max(0, len(value_lines) - 1) * (detail_size + 4)
      else:
        value_lines = [self._fit_llm_text(self._llm_advisory_font, value, value_size, value_w)]
        row_height = row_h
      render_rows.append((label, value_lines, row_height))

    panel_h = 70 + sum(row[2] for row in render_rows) + 16
    x = rect.x + (rect.width - panel_w) / 2
    y = rect.y + (rect.height - panel_h) * 0.42
    panel = rl.Rectangle(x, y, panel_w, panel_h)

    bg = rl.Color(6, 12, 16, 164)
    border = rl.Color(255, 186, 55, 215)
    accent = rl.Color(255, 205, 68, 235)
    label_color = rl.Color(143, 218, 235, 235)
    value_color = rl.Color(255, 255, 245, 245)
    detail_color = rl.Color(232, 240, 244, 235)

    rl.draw_rectangle_rounded(rl.Rectangle(x + 4, y + 4, panel_w, panel_h), 0.08, 8, rl.Color(0, 0, 0, 78))
    rl.draw_rectangle_rounded(panel, 0.08, 8, bg)
    rl.draw_rectangle_rounded_lines_ex(panel, 0.08, 8, 3, border)
    rl.draw_rectangle(int(x + 14), int(y + 16), 7, int(panel_h - 32), accent)

    inner_x = x + 34
    self._draw_shadowed_llm_text(self._llm_advisory_font, "ROAD INSPECTION FINDING",
                                 rl.Vector2(inner_x, y + 15), header_size, accent)

    row_y = y + 58
    value_x = inner_x + label_col_w
    value_w = max(0, int(x + panel_w - value_x - 22))
    for label, value_lines, row_height in render_rows:
      value_font_size = detail_size if label == "DETAIL" else value_size
      self._draw_shadowed_llm_text(self._llm_advisory_meta_font, label, rl.Vector2(inner_x, row_y + 7), label_size, label_color)
      for line_index, value_line in enumerate(value_lines):
        line_y = row_y + line_index * (value_font_size + 4)
        self._draw_shadowed_llm_text(self._llm_advisory_font, value_line, rl.Vector2(value_x, line_y), value_font_size,
                                     detail_color if label == "DETAIL" else value_color)
      row_y += row_height

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
    # Get camera configuration
    # TODO: cache with vEgo?
    calib_time = ui_state.sm.recv_frame['liveCalibration']
    current_dims = (self._content_rect.width, self._content_rect.height)
    device_camera = self.device_camera or DEFAULT_DEVICE_CAMERA
    is_wide_camera = self.stream_type == WIDE_CAM
    intrinsic = device_camera.ecam.intrinsics if is_wide_camera else device_camera.fcam.intrinsics
    calibration = self.view_from_wide_calib if is_wide_camera else self.view_from_calib
    if is_wide_camera:
      zoom = 0.7 * 1.5
    else:
      zoom = np.interp(ui_state.sm['carState'].vEgo, [10, 30], [0.8, 1.0])

    # Calculate transforms for vanishing point
    inf_point = np.array([1000.0, 0.0, 0.0])
    calib_transform = intrinsic @ calibration
    kep = calib_transform @ inf_point

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
        y_offset = np.clip((kep[1] / kep[2] - cy) * zoom + CAM_Y_OFFSET, -max_y_offset, max_y_offset)
      else:
        x_offset, y_offset = 0, 0
    except (ZeroDivisionError, OverflowError):
      x_offset, y_offset = 0, 0

    # Cache the computed transformation matrix to avoid recalculations
    self._last_calib_time = calib_time
    self._last_rect_dims = current_dims
    self._last_stream_type = self.stream_type
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
    self._model_renderer.set_transform(video_transform @ calib_transform)

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
