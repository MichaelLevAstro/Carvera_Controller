"""
Live camera view for the Makera Z1 / Z1 Pro.

The Z1's WiFi module (an ESP32) serves an MJPEG-over-WebSocket live stream on
port 82 at ``/ws_video``: the client sends the text message ``start_stream`` and
the server then pushes each camera frame as a single binary WebSocket message
containing raw JPEG bytes. This module connects to that stream and paints the
frames onto a Kivy ``Image`` widget. It mirrors what the vendor Z1 app does.

Notes / limitations:
- The camera is only reachable over WiFi (the ESP32 serves it). There is no
  camera path over the USB (framed) connection, so the stream is gated on a
  WiFi connection.
- The firmware exposes no camera parameter controls (exposure, gain, ...). The
  vendor app has none either, so this is view-only.

The WebSocket client is a tiny hand-rolled RFC 6455 client (plain ``ws://``, no
TLS) so the project keeps its dependency-light footprint — no ``websockets``
package to thread through the desktop/Android/iOS builds.
"""

import base64
import os
import socket
import struct
import threading
from io import BytesIO

from kivy.app import App
from kivy.clock import Clock
from kivy.core.image import Image as CoreImage
from kivy.factory import Factory
from kivy.graphics import RenderContext, Rectangle
from kivy.logger import Logger
from kivy.properties import NumericProperty, ObjectProperty
from kivy.uix.widget import Widget

from .Controller import CONN_WIFI

VIDEO_PORT = 82
VIDEO_PATH = '/ws_video'

# WebSocket opcodes
_OP_CONT = 0x0
_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class _WebSocket:
    """A minimal ws:// client: masked sends, unmasked reads, ping/pong, close."""

    def __init__(self, host, port, path, timeout=5.0):
        self._sock = socket.create_connection((host, port), timeout=timeout)
        self._sock.settimeout(1.0)
        self._buf = bytearray(self._handshake(host, port, path))

    def _handshake(self, host, port, path):
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            'GET %s HTTP/1.1\r\n'
            'Host: %s:%d\r\n'
            'Upgrade: websocket\r\n'
            'Connection: Upgrade\r\n'
            'Sec-WebSocket-Key: %s\r\n'
            'Sec-WebSocket-Version: 13\r\n'
            '\r\n' % (path, host, port, key)
        )
        self._sock.sendall(req.encode())
        data = b''
        while b'\r\n\r\n' not in data:
            chunk = self._sock.recv(1024)
            if not chunk:
                raise ConnectionError('WebSocket handshake closed by peer')
            data += chunk
        status_line = data.split(b'\r\n', 1)[0]
        if b' 101 ' not in status_line:
            raise ConnectionError('WebSocket handshake failed: %s'
                                  % status_line.decode('latin-1', 'replace'))
        # Any bytes past the header belong to the first frame(s).
        return data.split(b'\r\n\r\n', 1)[1]

    def _recv_exact(self, n):
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError('WebSocket closed by peer')
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def read_frame(self):
        """Return (fin, opcode, payload) for one frame. Raises socket.timeout."""
        b0, b1 = self._recv_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack('>H', self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack('>Q', self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else None
        payload = self._recv_exact(length) if length else b''
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        return fin, opcode, payload

    def send(self, opcode, payload=b''):
        if isinstance(payload, str):
            payload = payload.encode('utf-8')
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack('>H', length)
        else:
            header.append(0x80 | 127)
            header += struct.pack('>Q', length)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(header) + mask + masked)

    def close(self):
        try:
            self.send(_OP_CLOSE)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


# Fragment shader applying host-side brightness / contrast / gamma to the frame.
# The Z1 camera exposes no sensor exposure control, so this is a display-only
# approximation (it cannot recover fully clipped highlights).
_CAMERA_FS = '''
$HEADER$
uniform float brightness;
uniform float contrast;
uniform float gamma;
void main(void) {
    vec4 c = texture2D(texture0, tex_coord0);
    vec3 rgb = c.rgb * brightness;
    rgb = (rgb - 0.5) * contrast + 0.5;
    rgb = clamp(rgb, 0.0, 1.0);
    rgb = pow(rgb, vec3(1.0 / max(gamma, 0.01)));
    gl_FragColor = vec4(clamp(rgb, 0.0, 1.0), c.a) * frag_color;
}
'''


class CameraView(Widget):
    """Draws the live JPEG frame through a brightness/contrast/gamma shader.

    Set ``texture`` each frame; the frame is letterboxed to its own aspect ratio
    inside the widget so it is never distorted.
    """

    texture = ObjectProperty(None, allownone=True)
    brightness = NumericProperty(1.0)
    contrast = NumericProperty(1.0)
    gamma = NumericProperty(1.0)

    def __init__(self, **kwargs):
        self.canvas = RenderContext(use_parent_projection=True,
                                    use_parent_modelview=True)
        self.canvas.shader.fs = _CAMERA_FS
        super().__init__(**kwargs)
        with self.canvas:
            self._rect = Rectangle(pos=self.pos, size=self.size)
        self._update_uniforms()
        self.bind(pos=self._layout, size=self._layout, texture=self._layout,
                  brightness=self._update_uniforms,
                  contrast=self._update_uniforms, gamma=self._update_uniforms)

    def _update_uniforms(self, *args):
        self.canvas['brightness'] = float(self.brightness)
        self.canvas['contrast'] = float(self.contrast)
        self.canvas['gamma'] = float(self.gamma)
        self.canvas.ask_update()

    def _layout(self, *args):
        self._rect.texture = self.texture
        tw, th = self.texture.size if self.texture else (0, 0)
        w, h = self.size
        if tw and th and w and h:
            scale = min(w / float(tw), h / float(th))
            rw, rh = tw * scale, th * scale
            self._rect.size = (rw, rh)
            self._rect.pos = (self.x + (w - rw) / 2.0, self.y + (h - rh) / 2.0)
        else:
            self._rect.size = self.size
            self._rect.pos = self.pos


Factory.register('CameraView', cls=CameraView)


class VideoStreamManager:
    """Owns the live-view worker thread and paints frames onto ``video_image``."""

    def __init__(self, root):
        self.root = root                 # the Makera root widget
        self._ws = None
        self._thread = None
        self._running = False
        self._session = 0                # bumped on every connect/disconnect

    # -- public API ---------------------------------------------------------

    def is_connected(self):
        return self._running

    def toggle_video_stream(self):
        if self._running:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        if self._running:
            return
        controller = getattr(self.root, 'controller', None)
        if controller is None or controller.connection_type != CONN_WIFI:
            self._show_error('Camera requires a WiFi connection to the Z1.')
            return
        address = getattr(controller, 'connection_address', '') or ''
        host = address.split(':')[0]
        if not host:
            self._show_error('No machine address for the camera stream.')
            return
        self._session += 1
        token = self._session
        self._running = True
        self._set_connected(True)
        self._thread = threading.Thread(
            target=self._run, args=(host, token), daemon=True)
        self._thread.start()

    def disconnect(self):
        self._session += 1               # invalidate the active session
        self._running = False
        ws, self._ws = self._ws, None
        if ws is not None:
            ws.close()
        self._set_connected(False)

    # -- worker -------------------------------------------------------------

    def _run(self, host, token):
        pending = bytearray()      # reassembles a fragmented binary message
        in_binary = False
        ws = None
        try:
            ws = _WebSocket(host, VIDEO_PORT, VIDEO_PATH)
            self._ws = ws
            ws.send(_OP_TEXT, 'start_stream')
            Logger.info('VideoStream: connected to ws://%s:%d%s',
                        host, VIDEO_PORT, VIDEO_PATH)
            # Use the local ``ws`` (not self._ws) throughout so a newer session
            # replacing self._ws can never be read or torn down by this worker.
            while self._running and self._session == token:
                try:
                    fin, opcode, payload = ws.read_frame()
                except socket.timeout:
                    continue
                if opcode == _OP_CLOSE:
                    break
                elif opcode == _OP_PING:
                    ws.send(_OP_PONG, payload)
                elif opcode == _OP_BINARY:
                    if fin:
                        self._display_jpeg(bytes(payload))
                    else:
                        pending = bytearray(payload)
                        in_binary = True
                elif opcode == _OP_CONT and in_binary:
                    pending += payload
                    if fin:
                        self._display_jpeg(bytes(pending))
                        pending = bytearray()
                        in_binary = False
                # text frames (e.g. vlive "preempted") are ignored for view-only
        except (OSError, ConnectionError) as exc:
            if self._running and self._session == token:
                Logger.error('VideoStream: %s', exc)
                self._show_error('Camera stream error: %s' % exc)
        finally:
            if ws is not None:
                ws.close()
            # Only reset shared state if no newer session has taken over.
            if self._session == token:
                self._ws = None
                self._running = False
                self._set_connected(False)

    # -- UI helpers (always hop to the main thread) -------------------------

    def _display_jpeg(self, data):
        def apply(_dt):
            image = self.root.ids.get('video_image')
            if image is None:
                return
            try:
                texture = CoreImage(BytesIO(data), ext='jpg').texture
                # linear filtering so the 640x480 sensor frame upscales smoothly
                texture.mag_filter = 'linear'
                texture.min_filter = 'linear'
                image.texture = texture
            except Exception as exc:  # decode can fail on a partial/garbled frame
                Logger.warning('VideoStream: JPEG decode failed: %s', exc)
        Clock.schedule_once(apply, 0)

    def _set_connected(self, value):
        def apply(_dt):
            app = App.get_running_app()
            if app is not None:
                app.video_connected = value
        Clock.schedule_once(apply, 0)

    def _show_error(self, message):
        Logger.error('VideoStream: %s', message)
        Clock.schedule_once(
            lambda _dt: self.root.show_message_popup(message, False), 0)
