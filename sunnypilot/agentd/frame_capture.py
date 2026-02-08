import time

import cv2
import numpy as np

from cereal.visionipc import VisionIpcClient, VisionStreamType
from openpilot.common.swaglog import cloudlog

DEFAULT_WIDTH = 640
DEFAULT_HEIGHT = 480
DEFAULT_INTERVAL_S = 1.0
JPEG_QUALITY = 70


class FrameCapture:
  """Captures periodic JPEG snapshots from VisionIPC for the AI agent.

  Connects to the road camera via VisionIPC shared memory, grabs a frame
  at a configurable interval, converts from YUV to RGB, resizes, and
  encodes as JPEG. The main loop never blocks if no frame is available.
  """

  def __init__(self, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT,
               interval_s: float = DEFAULT_INTERVAL_S):
    self.width = width
    self.height = height
    self.interval_s = interval_s
    self.vipc: VisionIpcClient | None = None
    self.last_capture_time: float = 0.0
    self.last_jpeg: bytes | None = None

  def _ensure_connected(self) -> bool:
    """Lazily connect to VisionIPC. Returns True if connected."""
    if self.vipc is not None and self.vipc.is_connected():
      return True

    try:
      self.vipc = VisionIpcClient("camerad", VisionStreamType.VISION_STREAM_ROAD, True)
      self.vipc.connect(False)
      return self.vipc.is_connected()
    except Exception as e:
      cloudlog.warning(f"agentd: VisionIPC connect failed: {e}")
      return False

  def get_snapshot(self) -> bytes | None:
    """Return JPEG bytes if it's time for a new snapshot, else return cached.

    This is non-blocking: if no new frame is available or it's not time yet,
    the previously captured JPEG (or None) is returned immediately.
    """
    now = time.monotonic()
    if now - self.last_capture_time < self.interval_s:
      return self.last_jpeg

    if not self._ensure_connected():
      return self.last_jpeg

    try:
      buf = self.vipc.recv()
      if buf is None or buf.data is None:
        return self.last_jpeg

      # Convert YUV (NV12) to RGB
      yuv = np.frombuffer(buf.data, dtype=np.uint8)
      yuv = yuv.reshape((buf.height * 3 // 2, buf.width))
      rgb = cv2.cvtColor(yuv, cv2.COLOR_YUV2RGB_NV12)

      # Resize to target dimensions
      rgb_resized = cv2.resize(rgb, (self.width, self.height), interpolation=cv2.INTER_AREA)

      # Encode as JPEG
      encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]
      success, jpeg_buf = cv2.imencode('.jpg', rgb_resized, encode_params)

      if success:
        self.last_jpeg = jpeg_buf.tobytes()
        self.last_capture_time = now

    except Exception as e:
      cloudlog.warning(f"agentd: frame capture error: {e}")

    return self.last_jpeg
