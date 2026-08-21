import time

import pyray as rl

from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.system.ui.lib.application import FontWeight, gui_app
from openpilot.system.ui.lib.text_measure import measure_text_cached
from openpilot.system.ui.widgets import Widget


TRAFFIC_ADVISORY_PARAM = "TrafficAdvisory"
PARAM_REFRESH_INTERVAL = 0.5
DISPLAY_TTL = 120.0

LEFT_MARGIN = 18
TOP_MARGIN = 174
MAX_WIDTH = 620

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


def _distance_text(distance_m: float) -> str:
  miles = distance_m / 1609.344
  if miles < 0.15:
    return "nearby"
  if miles < 10.0:
    return f"{miles:.1f} mi"
  return f"{round(miles)} mi"


class TrafficAdvisoryOverlay(Widget):
  def __init__(self, top_margin: int = TOP_MARGIN):
    super().__init__()
    self._font_title = gui_app.font(FontWeight.SEMI_BOLD)
    self._font_body = gui_app.font(FontWeight.ROMAN)
    self._font_meta = gui_app.font(FontWeight.MEDIUM)

    self._top_margin = top_margin
    self._record: dict[str, object] | None = None
    self._last_param_check = 0.0
    self._alpha_filter = FirstOrderFilter(0.0, 0.12, 1 / gui_app.target_fps)

  def _update_state(self) -> None:
    now = time.monotonic()
    if now - self._last_param_check >= PARAM_REFRESH_INTERVAL:
      self._last_param_check = now
      record = ui_state.params.get(TRAFFIC_ADVISORY_PARAM)
      self._record = record if isinstance(record, dict) else None

    self._alpha_filter.update(1.0 if self._has_visible_advisory() else 0.0)

  def _has_visible_advisory(self) -> bool:
    if not self._record or not self._record.get("has_advisory"):
      return False
    timestamp = _float_value(self._record.get("timestamp"))
    return timestamp > 0.0 and time.time() - timestamp <= DISPLAY_TTL

  def _render(self, rect: rl.Rectangle) -> bool:
    alpha = self._alpha_filter.x
    if alpha <= 0.01 or not self._record:
      return False

    selected = self._selected()
    x = rect.x + LEFT_MARGIN
    y = rect.y + self._top_margin
    width = min(MAX_WIDTH, max(0, int(rect.width - LEFT_MARGIN * 2)))

    title = _clean_text(self._record.get("driver_display"), 72, "Traffic advisory ahead")
    detail = _clean_text(selected.get("message"), 150)
    lane = _clean_text(selected.get("lane_description"), 72)
    meta = self._meta_text(selected)

    self._draw_shadowed_text(self._font_title, title, TITLE_FONT_SIZE, x, y, width,
                             self._severity_color(selected, alpha), alpha)
    y += TITLE_FONT_SIZE + LINE_SPACING

    if detail:
      self._draw_shadowed_text(self._font_body, detail, DETAIL_FONT_SIZE, x, y, width,
                               rl.Color(255, 255, 255, int(210 * alpha)), alpha)
      y += DETAIL_FONT_SIZE + LINE_SPACING

    if lane:
      self._draw_shadowed_text(self._font_meta, lane, META_FONT_SIZE, x, y, width,
                               rl.Color(235, 235, 235, int(190 * alpha)), alpha)
      y += META_FONT_SIZE + LINE_SPACING

    if meta:
      self._draw_shadowed_text(self._font_meta, meta, META_FONT_SIZE, x, y, width,
                               rl.Color(215, 225, 235, int(185 * alpha)), alpha)

    return True

  def _selected(self) -> dict[str, object]:
    if not self._record:
      return {}
    selected = self._record.get("selected")
    return selected if isinstance(selected, dict) else {}

  def _meta_text(self, selected: dict[str, object]) -> str:
    pieces = []
    label = _clean_text(selected.get("label"), 24)
    severity = _clean_text(selected.get("severity"), 16)
    road = _clean_text(selected.get("roadway"), 32)
    direction = _clean_text(selected.get("direction"), 16)
    distance = _float_value(selected.get("distance_m"), -1.0)

    if label:
      pieces.append(label)
    if severity and severity != "unknown":
      pieces.append(severity)
    if road:
      pieces.append(road)
    if direction and direction != "both":
      pieces.append(direction)
    if distance >= 0.0:
      pieces.append(_distance_text(distance))
    return _clean_text(" | ".join(pieces), 110)

  def _severity_color(self, selected: dict[str, object], alpha: float) -> rl.Color:
    severity = _clean_text(selected.get("severity"), 16).lower()
    advisory_type = _clean_text(selected.get("type"), 24).lower()
    if severity in {"critical", "severe", "major"} or advisory_type == "closure":
      return rl.Color(255, 95, 76, int(235 * alpha))
    if severity in {"intermediate", "moderate", "medium"} or advisory_type in {"incident", "weather"}:
      return rl.Color(255, 210, 116, int(230 * alpha))
    return rl.Color(180, 226, 255, int(225 * alpha))

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
