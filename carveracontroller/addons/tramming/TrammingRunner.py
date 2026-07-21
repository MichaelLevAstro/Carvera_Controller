"""Host-driven tramming sweep.

Sweeps one axis back and forth between two machine-coordinate positions so the
operator can dial-in squareness with an indicator. Runs on a background thread:
send an absolute G53 feed move, wait for the machine to reach it (position
within tolerance and state Idle), then move to the other end. Repeats until the
stop event is set, the loop cap is reached, the machine alarms, or the app shuts
down (``controller.stop``).

Note: because moves are sent one leg at a time, pressing Stop halts the loop
after the current leg finishes travelling - no soft reset, so the machine is
never left in Alarm/Hold.
"""

import time
import logging

from ...CNC import CNC

logger = logging.getLogger(__name__)

AXIS_VAR = {'X': 'mx', 'Y': 'my', 'Z': 'mz', 'A': 'ma'}
POS_TOL = 0.05      # mm (linear) / deg (rotary)


class TrammingRunner:
    def __init__(self, controller, axis, minv, maxv, feed, loops,
                 stop_event, on_update, on_finish):
        self.c = controller
        self.axis = axis
        self.lo = min(minv, maxv)
        self.hi = max(minv, maxv)
        self.feed = feed
        self.loops = loops              # 0 => run until stopped
        self.stop = stop_event
        self.on_update = on_update
        self.on_finish = on_finish

    def run(self):
        reason = 'stopped'
        try:
            var = AXIS_VAR[self.axis]
            i = 0
            # first leg goes from the current position to the high end, then min
            while not self._should_stop():
                if self.loops and i >= self.loops:
                    reason = 'done'
                    break
                if not self._move_and_wait(var, self.hi):
                    reason = self._abort_reason()
                    break
                if self._should_stop():
                    break
                if not self._move_and_wait(var, self.lo):
                    reason = self._abort_reason()
                    break
                i += 1
                self.on_update(i)
        except Exception:
            logger.exception("tramming runner failed")
            reason = 'error'
        finally:
            self.on_finish(reason)

    def _should_stop(self):
        return self.stop.is_set() or self.c.stop.is_set()

    def _abort_reason(self):
        if self._should_stop():
            return 'stopped'
        if CNC.vars.get('state', '') == 'Alarm':
            return 'alarm'
        return 'timeout'

    def _move_and_wait(self, var, pos):
        start = CNC.vars.get(var, 0.0)
        self.c.executeCommand("G53 G1 %s%.3f F%.1f\n" % (self.axis, pos, self.feed))
        dist = abs(pos - start)
        # feed is per-minute: allow twice the nominal travel time plus a buffer
        if self.feed > 0:
            timeout = max(30.0, (dist / self.feed) * 60.0 * 2.0 + 10.0)
        else:
            timeout = 120.0
        end = time.time() + timeout
        while time.time() < end:
            if self._should_stop():
                return False
            if CNC.vars.get('state', '') == 'Alarm':
                return False
            if (abs(CNC.vars.get(var, 0.0) - pos) <= POS_TOL
                    and CNC.vars.get('state', '') == 'Idle'):
                return True
            time.sleep(0.05)
        return False
