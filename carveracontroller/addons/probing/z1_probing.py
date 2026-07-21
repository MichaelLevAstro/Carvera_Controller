"""
Host-side probing for the Makera Z1.

The community probing menu drives the Carvera community firmware's M460-M469
probing macros, which the stock Z1 firmware does not have. But the Z1 firmware
DOES provide its own probing:

  * M480.1-4  outside corner   (probe top, then each outer face at depth)
  * M480.5-8  inside corner
  * M480.9    bore  / in-pocket centre
  * M480.10   boss  / out-pocket centre
    - all take X/Y offset, Z depth, D probe diameter; they probe Z first then
      the sides, and set the work origin. This is exactly what Makera's own
      "Outside Corner picking" wizard uses.

So corner / bore / boss operations are translated to the matching M480 command
(firmware does the whole routine). Single-axis edge probing has no M480
equivalent, so it is reproduced with G38.2 + G10 L20. Angle probing is not
offered by Makera on the Z1; it is done host-side as an informational
measurement (the stock firmware has no WCS-rotation G-code to apply it).

`translate(gcode)` -> list of G-code lines to stream, or None.
`build_runner(gcode, controller)` -> callable for the (angle) live op, or None.
"""

import re
import time
import math
import logging

from ...CNC import CNC

logger = logging.getLogger(__name__)

_MCODE_RE = re.compile(r'\s*M(\d+)(?:\.(\d+))?')
_PARAM_RE = re.compile(r'([A-Za-z])\s*(-?\d*\.?\d+)')

DEFAULT_FEED = 300.0        # mm/min, firmware feed_rate default
DEFAULT_RETRACT = 1.5       # mm, firmware retract_distance default
DEFAULT_TIP_DIA = 2.0       # mm, fallback probe tip diameter
DEFAULT_OFFSET = 20.0       # mm, M480 X/Y offset default
DEFAULT_DEPTH = 2.0         # mm, M480 Z depth default
PROBE_TIMEOUT = 90.0        # s

RUNNER_MCODES = {465: 'angle'}


def parse(gcode):
    """Return (mcode, subcode, {LETTER: float})."""
    m = _MCODE_RE.match(gcode)
    if not m:
        return None, None, {}
    mcode = int(m.group(1))
    subcode = int(m.group(2)) if m.group(2) else 0
    params = {letter.upper(): float(value)
              for letter, value in _PARAM_RE.findall(gcode[m.end():])}
    return mcode, subcode, params


def _probe_subcodes(params):
    """(probe, traverse) G38 subcodes; shifted +2 for a normally-closed probe."""
    invert = params.get('I', 0) > 0
    return (4, 5) if invert else (2, 3)


# ---------------------------------------------------------------------------
# Single-axis edge probe (no M480 equivalent) -> host-side G38.2 script
# ---------------------------------------------------------------------------
def _single_axis(params):
    probe_sub, _ = _probe_subcodes(params)
    feed = params.get('F', DEFAULT_FEED)
    retract = abs(params.get('R', DEFAULT_RETRACT))
    radius = params.get('D', DEFAULT_TIP_DIA) / 2.0
    axis = None
    distance = 0.0
    for a in ('X', 'Y', 'Z'):
        if abs(params.get(a, 0.0)) > 1e-6:
            axis, distance = a, params[a]
            break
    if axis is None:
        return None
    sign = 1.0 if distance >= 0 else -1.0
    back = -sign * retract
    offset = 0.0 if axis == 'Z' else -sign * radius   # Z probes the tip end
    return [
        "G90",
        "G38.%d %s%.3f F%.1f" % (probe_sub, axis, distance, feed),
        "G91 G0 %s%.3f" % (axis, back),
        "G90",
        "G38.%d %s%.3f" % (probe_sub, axis, distance),
        "G10 L20 P0 %s%.3f" % (axis, offset),
        "G91 G0 %s%.3f" % (axis, back),
        "G90",
    ]


# ---------------------------------------------------------------------------
# Corner / bore / boss -> native M480 command
# ---------------------------------------------------------------------------
def _corner_subcode(x, y, base):
    """Map the community operation's signed X/Y to the M480 corner subcode.

    The community operations encode the corner in the sign of X/Y (via
    apply_direction). Cross-referenced with Makera's own corner->subcode map
    (top-left=1, top-right=2, bottom-right=3, bottom-left=4; inside = +4):

        (+X, -Y) top-left     -> base+0
        (-X, -Y) top-right    -> base+1
        (-X, +Y) bottom-right -> base+2
        (+X, +Y) bottom-left  -> base+3

    Note the firmware's M480 routine is asymmetric (X moves -X_distance then
    probes +X_distance; Y moves +Y_distance then probes -Y_distance), so the
    corner cannot be derived by a naive same-sign rule.
    """
    xpos = x >= 0
    ypos = y >= 0
    if xpos and not ypos:
        off = 0     # top-left
    elif not xpos and not ypos:
        off = 1     # top-right
    elif not xpos and ypos:
        off = 2     # bottom-right
    else:
        off = 3     # bottom-left (+X, +Y)
    return base + off


def _m480_corner(subcode, params):
    """Corner probe: both X and Y are always probed."""
    x = abs(params.get('X', DEFAULT_OFFSET)) or DEFAULT_OFFSET
    y = abs(params.get('Y', DEFAULT_OFFSET)) or DEFAULT_OFFSET
    depth = abs(params.get('E', params.get('H', DEFAULT_DEPTH))) or DEFAULT_DEPTH
    dia = params.get('D', DEFAULT_TIP_DIA)
    return ["M480.%d X%.3f Y%.3f Z%.3f D%.3f" % (subcode, x, y, depth, dia)]


def _m480_center(subcode, params):
    """Bore/boss centre probe. Only the axes the operation kept are probed;
    a 0 distance tells the firmware (M480.9/.10) to skip that axis, which is how
    the X-only / Y-only variants work."""
    x = abs(params.get('X', 0.0))
    y = abs(params.get('Y', 0.0))
    depth = abs(params.get('H', DEFAULT_DEPTH)) or DEFAULT_DEPTH
    dia = params.get('D', DEFAULT_TIP_DIA)
    return ["M480.%d X%.3f Y%.3f Z%.3f D%.3f" % (subcode, x, y, depth, dia)]


def translate(gcode):
    """Operations reproducible as streamed G-code -> list of lines, or None."""
    mcode, subcode, params = parse(gcode)
    if subcode != 0:
        return None
    if mcode == 466:
        return _single_axis(params)
    if mcode == 464:  # outside corner
        return _m480_corner(_corner_subcode(params.get('X', 0.0), params.get('Y', 0.0), 1), params)
    if mcode == 463:  # inside corner
        return _m480_corner(_corner_subcode(params.get('X', 0.0), params.get('Y', 0.0), 5), params)
    if mcode == 461:  # bore (in-pocket centre)
        return _m480_center(9, params)
    if mcode == 462:  # boss (out-pocket centre)
        return _m480_center(10, params)
    return None


def preview_text(gcode):
    mcode, _, _ = parse(gcode)
    if RUNNER_MCODES.get(mcode) == 'angle':
        return ("Angle: probe two points along an edge and report the measured "
                "angle (the Z1 firmware cannot store a WCS rotation).")
    return "Runs on the Z1 as a host-side probing sequence."


def build_runner(gcode, controller):
    mcode, subcode, params = parse(gcode)
    if subcode != 0 or mcode not in RUNNER_MCODES:
        return None
    return Z1ProbeRunner(controller, params).run_angle


def build_bore_center_runner(gcode, controller):
    """Multi-point bore-centre probe (least-squares circle fit).

    Applies to the full round-bore case (M461 with both X and Y) on ANY
    machine - it only uses G38.2 + [PRB] + G10, which work on both the raw and
    framed protocols. Returns a callable, or None (leaving the X-only/Y-only
    slot variants to their normal per-axis handling).
    """
    mcode, subcode, params = parse(gcode)
    if mcode != 461 or subcode != 0:
        return None
    if abs(params.get('X', 0.0)) <= 0 or abs(params.get('Y', 0.0)) <= 0:
        return None
    return Z1ProbeRunner(controller, params).run_bore_center


def bore_preview_text(gcode):
    _, _, params = parse(gcode)
    n = max(3, int(params.get('P', 4) or 4))
    return ("Bore centre (multi-point): probe %d points around the bore, fit a "
            "circle, and set the origin at the fitted centre." % n)


# ---------------------------------------------------------------------------
# Least-squares circle fit (Kasa), pure Python
# ---------------------------------------------------------------------------
def _det3(m):
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def _solve3(M, b):
    d = _det3(M)
    if abs(d) < 1e-12:
        return None
    out = []
    for i in range(3):
        Mi = [row[:] for row in M]
        for r in range(3):
            Mi[r][i] = b[r]
        out.append(_det3(Mi) / d)
    return out


def fit_circle(points):
    """Return (cx, cy, radius) best-fit circle for >=3 (x, y) points, or None."""
    n = len(points)
    if n < 3:
        return None
    Sx = Sy = Sxx = Syy = Sxy = Sz = Szx = Szy = 0.0
    for x, y in points:
        z = x * x + y * y
        Sx += x; Sy += y; Sxx += x * x; Syy += y * y; Sxy += x * y
        Sz += z; Szx += z * x; Szy += z * y
    sol = _solve3([[Sxx, Sxy, Sx], [Sxy, Syy, Sy], [Sx, Sy, float(n)]],
                  [-Szx, -Szy, -Sz])
    if sol is None:
        return None
    a, b, c = sol
    cx, cy = -a / 2.0, -b / 2.0
    r2 = cx * cx + cy * cy - c
    if r2 <= 0:
        return None
    return cx, cy, math.sqrt(r2)


# ---------------------------------------------------------------------------
# Live runner (angle only)
# ---------------------------------------------------------------------------
class Z1ProbeRunner:
    def __init__(self, controller, params):
        self.c = controller
        self.p = params
        self.probe_sub, self.traverse_sub = _probe_subcodes(params)
        self.feed = params.get('F', DEFAULT_FEED)
        self.retract = abs(params.get('R', DEFAULT_RETRACT))
        self.start = None

    def _send(self, line):
        self.c.executeCommand(line + "\n")

    def _error(self, msg):
        self.c.log.put((self.c.MSG_ERROR, msg))
        logger.error("Z1 probing: %s", msg)

    def _info(self, msg):
        self.c.log.put((self.c.MSG_NORMAL, msg))

    def _capture_start(self):
        self.start = (CNC.vars['mx'], CNC.vars['my'], CNC.vars['mz'])

    def _goto_mcs(self, x=None, y=None):
        parts = ""
        if x is not None:
            parts += " X%.3f" % x
        if y is not None:
            parts += " Y%.3f" % y
        if parts:
            self._send("G53 G0" + parts)

    def _wait_prb(self, baseline_count, timeout=PROBE_TIMEOUT):
        """Wait for the next probe result and return it (mx, my, mz, ok), or None.

        A probe (G38) is the only thing that increments prb_count; repositioning
        (G53/G0) moves emit no [PRB]. So waiting for the count to advance detects
        THIS probe's completion, even if a return-to-start move runs first - it
        can't be fooled by an intervening move the way idle-watching could.
        """
        end = time.time() + timeout
        while time.time() < end:
            if self.c.stop.is_set():
                return None
            if self.c.prb_count > baseline_count:
                return self.c.last_prb
            if CNC.vars.get('state', '') == 'Alarm':
                return None
            time.sleep(0.02)
        return None

    def _probe(self, axis, distance):
        """Probe one axis; return the [PRB] tuple (mx,my,mz,ok) or None."""
        sign = 1.0 if distance >= 0 else -1.0
        baseline = self.c.prb_count
        self._send("G90")
        self._send("G38.%d %s%.3f F%.1f" % (self.probe_sub, axis, distance, self.feed))
        prb = self._wait_prb(baseline)
        if prb is None or prb[3] == 0:
            return None
        self._send("G91 G0 %s%.3f" % (axis, -sign * self.retract))
        self._send("G90")
        return prb

    def _probe_xy(self, dx, dy):
        """Diagonal probe by (dx, dy); return the [PRB] tuple or None.

        Uses the no-error probe variant (G38.3/.5) so that a miss (e.g. probe
        distance shorter than the bore radius) aborts the sequence cleanly
        instead of halting the machine.
        """
        baseline = self.c.prb_count
        self._send("G90")
        self._send("G38.%d X%.3f Y%.3f F%.1f" % (self.traverse_sub, dx, dy, self.feed))
        prb = self._wait_prb(baseline)
        if prb is None or prb[3] == 0:
            return None
        return prb

    def run_bore_center(self):
        """Probe N points around a bore, fit a circle, set the origin at the centre."""
        n = max(3, int(self.p.get('P', 4) or 4))
        reach = max(abs(self.p.get('X', 0.0)), abs(self.p.get('Y', 0.0)))
        if reach <= 0:
            return self._error("Bore centre needs an X or Y probe distance.")
        self._capture_start()
        sx, sy = self.start[0], self.start[1]
        pts = []
        for i in range(n):
            ang = 2.0 * math.pi * i / n
            prb = self._probe_xy(reach * math.cos(ang), reach * math.sin(ang))
            if prb is None:
                return self._error("Bore probe %d/%d did not contact." % (i + 1, n))
            pts.append((prb[0], prb[1]))
            self._goto_mcs(x=sx, y=sy)   # back to the start point for the next angle
        fit = fit_circle(pts)
        if fit is None:
            return self._error("Bore circle fit failed.")
        cx, cy, radius = fit
        self._goto_mcs(x=cx, y=cy)          # G10 (queued after) sets origin at the centre
        self._send("G10 L20 P0 X0 Y0")
        roundness = max(abs(math.hypot(px - cx, py - cy) - radius) for px, py in pts)
        self._info("Bore centre set: diameter %.3f mm, roundness %.3f mm (%d points)"
                   % (2 * radius, 2 * roundness, n))

    def run_angle(self):
        self._capture_start()
        x = self.p.get('X', 0.0)
        y = self.p.get('Y', 0.0)
        span = self.p.get('E', 0.0)
        if abs(span) < 1e-6:
            return self._error("Angle needs a probe depth (E).")
        if abs(x) > 1e-6:
            probe_axis, probe_dist, along_axis, along, idx = 'Y', x, 'X', span, 1
        elif abs(y) > 1e-6:
            probe_axis, probe_dist, along_axis, along, idx = 'X', y, 'Y', span, 0
        else:
            return self._error("Angle needs X or Y.")
        p1 = self._probe(probe_axis, probe_dist)
        if p1 is None:
            return self._error("Angle: first point not contacted.")
        self._goto_mcs(x=self.start[0], y=self.start[1])
        self._send("G91 G0 %s%.3f" % (along_axis, along))
        self._send("G90")
        p2 = self._probe(probe_axis, probe_dist)
        if p2 is None:
            return self._error("Angle: second point not contacted.")
        self._goto_mcs(x=self.start[0], y=self.start[1])
        angle = math.degrees(math.atan2(p2[idx] - p1[idx], along))
        self._info("Measured edge angle: %.3f deg" % angle)
