import time

import pyray as rl

from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget


ROAD_INSPECTION_PARAM = "LLMRoadInspection"
PARAM_REFRESH_INTERVAL = 0.5
DISPLAY_TTL = 18.0
MIN_DISPLAY_CONFIDENCE = 0.35

LEFT_MARGIN = 18
TOP_MARGIN = 82
MAX_WIDTH = 560

TITLE_FONT_SIZE = 30
DETAIL_FONT_SIZE = 21
META_FONT_SIZE = 18
LINE_SPACING = 5


def _clean_text(value: object, max_len: int, default: str = "") -> str:
  if value is None:
    return default
  text = " ".join(str(value).replace("\n", " ").split())
  return text[:max_len] if text else default


def _float_value(value: object, default: float = 0.0) -> float:
  try:
    return float(value)
  except (TypeError, ValueError):
    return default


class RoadInspectionOverlay(Widget):
  def __init__(self):
    super().__init__()
    self._font_title = gui_app.font(FontWeight.SEMI_BOLD)
    self._font_body = gui_app.font(FontWeight.ROMAN)
    self._font_meta = gui_app.font(FontWeight.MEDIUM)

    self._record: dict[str, object] | None = None
    self._last_param_check = 0.0
    self._alpha_filter = FirstOrderFilter(0.0, 0.12, 1 / gui_app.target_fps)

  def _update_state(self) -> None:
    now = time.monotonic()
    if now - self._last_param_check >= PARAM_REFRESH_INTERVAL:
      self._last_param_check = now
      record = ui_state.params.get(ROAD_INSPECTION_PARAM)
      self._record = record if isinstance(record, dict) else None

    self._alpha_filter.update(1.0 if self._has_visible_issue() else 0.0)

  def _has_visible_issue(self) -> bool:
    if not self._record or not self._record.get("found_issue"):
      return False

    confidence = _float_value(self._record.get("confidence"))
    timestamp = _float_value(self._record.get("timestamp"))
    if confidence < MIN_DISPLAY_CONFIDENCE or timestamp <= 0.0:
      return False

    return time.time() - timestamp <= DISPLAY_TTL

  def _render(self, rect: rl.Rectangle) -> bool:
    alpha = self._alpha_filter.x
    if alpha <= 0.01 or not self._record:
      return False

    x = rect.x + LEFT_MARGIN
    y = rect.y + TOP_MARGIN
    width = min(MAX_WIDTH, max(0, int(rect.width - LEFT_MARGIN * 2)))

    title = self._title_text()
    detail = _clean_text(self._record.get("description"), 120)
    meta = self._meta_text()

    self._draw_shadowed_text(self._font_title, title, TITLE_FONT_SIZE, x, y, width,
                             self._severity_color(alpha), alpha)
    y += TITLE_FONT_SIZE + LINE_SPACING

    if detail:
      self._draw_shadowed_text(self._font_body, detail, DETAIL_FONT_SIZE, x, y, width,
                               rl.Color(255, 255, 255, int(210 * alpha)), alpha)
      y += DETAIL_FONT_SIZE + LINE_SPACING

    if meta:
      self._draw_shadowed_text(self._font_meta, meta, META_FONT_SIZE, x, y, width,
                               rl.Color(215, 225, 235, int(185 * alpha)), alpha)

    return True

  def _title_text(self) -> str:
    title = _clean_text(self._record.get("driver_display") if self._record else None, 72)
    if title:
      return title

    location = self._record.get("location") if self._record else None
    location_text = ""
    if isinstance(location, dict):
      lane_position = _clean_text(location.get("lane_position"), 32)
      side = _clean_text(location.get("side"), 16)
      location_text = lane_position if lane_position and lane_position != "unknown" else side

    category = _clean_text(self._record.get("damage_category") if self._record else None, 28, "Road issue")
    severity = _clean_text(self._record.get("severity") if self._record else None, 16)
    pieces = [category]
    if location_text and location_text != "unknown":
      pieces.append(location_text)
    if severity and severity != "unknown":
      pieces.append(severity)
    return _clean_text(" - ".join(pieces), 72, "Road issue")

  def _meta_text(self) -> str:
    if not self._record:
      return ""

    element = _clean_text(self._record.get("inspection_element"), 32)
    feature = _clean_text(self._record.get("asset_feature"), 48)
    confidence = round(_float_value(self._record.get("confidence")) * 100)
    action = _clean_text(self._record.get("recommended_action"), 44)

    pieces = []
    if element:
      pieces.append(element)
    if feature and feature != "unknown":
      pieces.append(feature)
    if confidence > 0:
      pieces.append(f"{confidence}%")
    if action and action != "none":
      pieces.append(action)
    return _clean_text(" | ".join(pieces), 100)

  def _severity_color(self, alpha: float) -> rl.Color:
    severity = _clean_text(self._record.get("severity") if self._record else None, 16).lower()
    if severity == "severe":
      return rl.Color(255, 95, 76, int(235 * alpha))
    if severity == "moderate":
      return rl.Color(255, 210, 116, int(230 * alpha))
    return rl.Color(255, 255, 255, int(225 * alpha))

  def _draw_shadowed_text(self, font: rl.Font, text: str, font_size: int, x: float, y: float,
                          width: int, color: rl.Color, alpha: float) -> None:
    display_text = self._elide(font, text, font_size, width)
    shadow = rl.Color(0, 0, 0, int(190 * alpha))

    for dx, dy in ((2, 2), (1, 1)):
      rl.draw_text_ex(font, display_text, rl.Vector2(x + dx, y + dy), font_size, 0, shadow)
    rl.draw_text_ex(font, display_text, rl.Vector2(x, y), font_size, 0, color)

  def _elide(self, font: rl.Font, text: str, font_size: int, width: int) -> str:
    if width <= 0:
      return ""
    if measure_text_cached(font, text, font_size).x <= width:
      return text

    ellipsis = "..."
    left, right = 0, len(text)
    while left < right:
      mid = (left + right) // 2
      candidate = text[:mid] + ellipsis
      if measure_text_cached(font, candidate, font_size).x <= width:
        left = mid + 1
      else:
        right = mid
    return text[:left - 1] + ellipsis if left > 0 else ellipsis
