"""Small CD+G state machine and PyQt presentation widget."""
from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PyQt6 import QtCore, QtGui, QtWidgets

try:
    import numpy as np
except Exception:
    np = None

WIDTH, HEIGHT = 300, 216
VISIBLE_X, VISIBLE_Y, VISIBLE_W, VISIBLE_H = 6, 12, 288, 192
PACKETS_PER_SECOND = 300


@dataclass
class _Snapshot:
    packet: int
    pixels: bytes
    palette: Tuple[Tuple[int, int, int], ...]
    transparent: int


class CdgDocument:
    def __init__(self, data: bytes, checkpoint_packets: int = 1500):
        self.packets = [data[i:i + 24] for i in range(0, len(data) - 23, 24)]
        self.checkpoint_packets = max(300, checkpoint_packets)
        self._checkpoints: List[_Snapshot] = []
        # Counted, not logged per-packet (see playback.cdg_parse_warning in
        # window.py's _on_karaoke_prepared -- one aggregate diagnostics
        # call per prepared document, not one per packet/frame).
        self.unrecognized_packet_count = 0
        pixels, palette, transparent = self._new_state()
        self._checkpoints.append(_Snapshot(0, bytes(pixels), tuple(palette), transparent))
        for index, packet in enumerate(self.packets, 1):
            # Counted here (the one-time initial parse), not inside
            # _apply() -- that method is reused by image_at()'s replay
            # logic, where the same packet can legitimately be applied
            # again (a seek), and re-counting it there would inflate the
            # total every time the user seeks.
            if (packet[0] & 0x3F) != 0x09:
                self.unrecognized_packet_count += 1
            transparent = self._apply(packet, pixels, palette, transparent)
            if index % self.checkpoint_packets == 0:
                self._checkpoints.append(_Snapshot(index, bytes(pixels), tuple(palette), transparent))
        self._checkpoint_numbers = [item.packet for item in self._checkpoints]
        # Cache of the last frame actually rendered (packet index, state) --
        # sequential forward playback (the common case, called every tick)
        # then only has to replay the handful of packets since the last
        # call instead of up to a full checkpoint interval every time
        # (measured ~36ms worst case replaying from checkpoint on every
        # tick -- a real GUI-thread stutter risk). A backward seek or a
        # jump past the next checkpoint still falls back to the nearest
        # checkpoint, same as before.
        self._last_render: Optional[_Snapshot] = None

    @classmethod
    def from_file(cls, path: str):
        with open(path, "rb") as handle:
            return cls(handle.read())

    @staticmethod
    def _new_state():
        return bytearray(WIDTH * HEIGHT), [(0, 0, 0)] * 16, -1

    def _apply(self, packet, pixels, palette, transparent):
        if len(packet) != 24 or (packet[0] & 0x3F) != 0x09:
            return transparent
        instruction = packet[1] & 0x3F
        data = packet[4:20]
        if instruction == 1:  # memory preset
            pixels[:] = bytes([data[0] & 0x0F]) * len(pixels)
        elif instruction == 2:  # border preset
            colour = data[0] & 0x0F
            for y in range(HEIGHT):
                for x in range(WIDTH):
                    if x < 6 or x >= 294 or y < 12 or y >= 204:
                        pixels[y * WIDTH + x] = colour
        elif instruction in (6, 38):  # tile / XOR tile
            c0, c1 = data[0] & 15, data[1] & 15
            y0, x0 = (data[2] & 31) * 12, (data[3] & 63) * 6
            for row in range(12):
                if y0 + row >= HEIGHT:
                    continue
                bits = data[4 + row] & 0x3F
                for col in range(6):
                    if x0 + col >= WIDTH:
                        continue
                    colour = c1 if bits & (1 << (5 - col)) else c0
                    pos = (y0 + row) * WIDTH + x0 + col
                    pixels[pos] = (pixels[pos] ^ colour) & 15 if instruction == 38 else colour
        elif instruction in (20, 24):  # scroll preset/copy
            colour = data[0] & 15
            hcmd, hoff = (data[1] >> 4) & 3, data[1] & 7
            vcmd, voff = (data[2] >> 4) & 3, data[2] & 15
            dx = -6 if hcmd == 1 else 6 if hcmd == 2 else 0
            dy = -12 if vcmd == 1 else 12 if vcmd == 2 else 0
            if dx or dy:
                old = bytes(pixels)
                for y in range(HEIGHT):
                    for x in range(WIDTH):
                        sx, sy = x - dx, y - dy
                        if instruction == 24:
                            sx %= WIDTH; sy %= HEIGHT
                        pixels[y * WIDTH + x] = old[sy * WIDTH + sx] if 0 <= sx < WIDTH and 0 <= sy < HEIGHT else colour
        elif instruction == 28:
            transparent = data[0] & 15
        elif instruction in (30, 31):
            start = 0 if instruction == 30 else 8
            for i in range(8):
                value = ((data[i * 2] & 0x3F) << 6) | (data[i * 2 + 1] & 0x3F)
                palette[start + i] = (((value >> 8) & 15) * 17, ((value >> 4) & 15) * 17, (value & 15) * 17)
        return transparent

    def image_at(self, position_ms: int) -> QtGui.QImage:
        target = min(len(self.packets), max(0, int(position_ms * PACKETS_PER_SECOND / 1000)))
        last = self._last_render
        if last is not None and last.packet <= target:
            # Forward from where we already are -- the common case during
            # normal playback, replaying only the packets since the last
            # rendered frame instead of since the last checkpoint.
            start = last.packet
            pixels, palette, transparent = bytearray(last.pixels), list(last.palette), last.transparent
        else:
            # A backward seek, a restart, or the very first frame -- fall
            # back to the nearest checkpoint at or before the target.
            checkpoint_index = max(0, bisect.bisect_right(self._checkpoint_numbers, target) - 1)
            snapshot = self._checkpoints[checkpoint_index]
            start = snapshot.packet
            pixels, palette, transparent = bytearray(snapshot.pixels), list(snapshot.palette), snapshot.transparent
        for packet in self.packets[start:target]:
            transparent = self._apply(packet, pixels, palette, transparent)
        self._last_render = _Snapshot(target, bytes(pixels), tuple(palette), transparent)
        rgba = self._build_rgba(pixels, palette, transparent)
        return QtGui.QImage(rgba, VISIBLE_W, VISIBLE_H, VISIBLE_W * 4, QtGui.QImage.Format.Format_ARGB32).copy()

    def _build_rgba(self, pixels, palette, transparent) -> bytes:
        # This runs once per rendered frame regardless of how many CDG
        # packets were replayed to get here -- a plain Python loop over
        # VISIBLE_W*VISIBLE_H (55296) pixels measured ~18ms on its own,
        # enough on its own to be a GUI-thread stutter risk at normal tick
        # rates. numpy vectorises the palette lookup instead.
        if np is not None:
            plane = np.frombuffer(bytes(pixels), dtype=np.uint8).reshape(HEIGHT, WIDTH)
            visible = plane[VISIBLE_Y:VISIBLE_Y + VISIBLE_H, VISIBLE_X:VISIBLE_X + VISIBLE_W]
            palette_bgr = np.array(
                [(b, g, r) for (r, g, b) in palette], dtype=np.uint8,
            )
            bgr = palette_bgr[visible]
            alpha = np.where(visible == transparent, 0, 255).astype(np.uint8)
            return np.dstack((bgr, alpha)).tobytes()
        rgba = bytearray(VISIBLE_W * VISIBLE_H * 4)
        offset = 0
        for y in range(VISIBLE_Y, VISIBLE_Y + VISIBLE_H):
            row = y * WIDTH
            for x in range(VISIBLE_X, VISIBLE_X + VISIBLE_W):
                index = pixels[row + x]
                r, g, b = palette[index]
                rgba[offset:offset + 4] = bytes((b, g, r, 0 if index == transparent else 255))
                offset += 4
        return bytes(rgba)


class CdgWidget(QtWidgets.QWidget):
    double_clicked = QtCore.pyqtSignal()
    escape_pressed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._document = None
        self._image = QtGui.QImage()
        self.setStyleSheet("background:#000000;")
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)

    def set_document(self, document):
        self._document = document
        self.set_position(0)

    def clear(self):
        self._document = None
        self._image = QtGui.QImage()
        self.update()

    def set_position(self, position_ms: int):
        if self._document is not None:
            self._image = self._document.image_at(position_ms)
            self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.fillRect(self.rect(), QtGui.QColor("black"))
        if self._image.isNull():
            return
        scaled = self._image.size().scaled(self.size(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        target = QtCore.QRect(QtCore.QPoint(0, 0), scaled)
        target.moveCenter(self.rect().center())
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, False)
        painter.drawImage(target, self._image)

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Escape:
            self.escape_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)
