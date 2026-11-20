from unittest import TestCase

import numpy as np

from flowhmr.multishot.fixed_camera_refinement import _quality_mask, _verify_steps


class CameraRefinementTest(TestCase):
    def test_requires_an_improvement_on_unfitted_frames_and_keeps_previous_coverage(self):
        previous = np.r_[False, np.ones(28, dtype=bool), False]
        holdout = np.arange(30) % 2 == 1
        span = np.ones(30) * 300
        before = np.ones((30, 12)) * 70
        fitted = np.where(holdout[:, None], before, 0.)
        self.assertIsNone(_quality_mask(fitted, span, holdout, previous, before))
        mask, old, new = _quality_mask(before * .5, span, holdout, previous, before)
        np.testing.assert_array_equal(mask, previous)
        self.assertEqual((old['p90_px'], new['p90_px']), (70., 35.))

    def test_fast_move_needs_background_support_at_that_time(self):
        rt = np.tile(np.eye(4), (7, 1, 1))
        rt[3:, 0, 3] = 1.
        K = np.tile(np.eye(3), (7, 1, 1))
        self.assertFalse(_verify_steps(rt, K, {'verified_step_ranges': [[4, 6]]}))
        self.assertTrue(_verify_steps(rt, K, {'verified_step_ranges': [[1, 4]]}))
