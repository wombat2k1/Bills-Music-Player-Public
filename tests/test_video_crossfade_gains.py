"""Pure math coverage for Stage C's crossfade gain curves
(GpuDualDeckVideoSubprocessController._crossfade_gains) -- no Qt objects
involved, just the static formula, so these run with zero Qt/subprocess
overhead."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import math

from billsmusic.video_subprocess import GpuDualDeckVideoSubprocessController as Gpu


def test_equal_power_endpoints():
    a0, b0 = Gpu._crossfade_gains(0.0, "Equal Power")
    assert a0 == 1.0
    assert b0 == 0.0
    a1, b1 = Gpu._crossfade_gains(1.0, "Equal Power")
    assert abs(a1 - 0.0) < 1e-9
    assert abs(b1 - 1.0) < 1e-9


def test_equal_power_midpoint_is_not_half_half():
    a, b = Gpu._crossfade_gains(0.5, "Equal Power")
    # cos(pi/4) == sin(pi/4) == sqrt(2)/2 ~= 0.7071, not 0.5 -- this is
    # exactly the point of equal power over linear: at the midpoint both
    # decks are still audible at ~71% amplitude each, not 50%.
    assert abs(a - math.sqrt(2) / 2) < 1e-9
    assert abs(b - math.sqrt(2) / 2) < 1e-9
    assert a > 0.5 and b > 0.5


def test_equal_power_constant_power_throughout():
    # a_gain^2 + b_gain^2 == 1 at every progress -- constant *power*,
    # unlike a linear fade where a_gain + b_gain == 1 (constant amplitude
    # sum, which is what produces the audible midpoint dip).
    for progress in (0.0, 0.1, 0.25, 0.4, 0.5, 0.6, 0.75, 0.9, 1.0):
        a, b = Gpu._crossfade_gains(progress, "Equal Power")
        assert abs((a * a + b * b) - 1.0) < 1e-9


def test_linear_endpoints_and_midpoint():
    a0, b0 = Gpu._crossfade_gains(0.0, "Linear")
    assert a0 == 1.0
    assert b0 == 0.0
    a1, b1 = Gpu._crossfade_gains(1.0, "Linear")
    assert a1 == 0.0
    assert b1 == 1.0
    a_mid, b_mid = Gpu._crossfade_gains(0.5, "Linear")
    assert a_mid == 0.5
    assert b_mid == 0.5


def test_linear_constant_amplitude_sum():
    for progress in (0.0, 0.25, 0.5, 0.75, 1.0):
        a, b = Gpu._crossfade_gains(progress, "Linear")
        assert abs((a + b) - 1.0) < 1e-9


def test_progress_is_clamped():
    a, b = Gpu._crossfade_gains(-0.5, "Equal Power")
    assert a == 1.0 and b == 0.0
    a, b = Gpu._crossfade_gains(1.5, "Equal Power")
    assert abs(a) < 1e-9 and abs(b - 1.0) < 1e-9


def test_unknown_curve_falls_back_to_equal_power():
    a, b = Gpu._crossfade_gains(0.5, "Some Future Curve")
    assert abs(a - math.sqrt(2) / 2) < 1e-9
    assert abs(b - math.sqrt(2) / 2) < 1e-9
