from types import SimpleNamespace
import unittest

import numpy as np
from scipy.spatial.transform import Rotation

from flowhmr.multishot.wrist_alignment import (
    ARM_BLEND_STEP, ARM_UPDATE_STEP, WRIST_CORRECTION_ACCEL, _bounded_arm_weights,
    _continuous_arm_solution, _continuous_wrist_observations, correct_wrists,
)


class WristContinuityTest(unittest.TestCase):
    def test_checks_both_sides_of_an_unexplained_wrist_step(self):
        observed = np.zeros((7, 3, 2))
        observed[:, 2, 0] = [0, 2, 4, 70, 73, 75, 77]
        predicted = observed.copy()
        predicted[:, 2, 0] = np.arange(7) * 2
        measured = np.ones(7, dtype=bool)
        span = np.full(7, 100.)
        np.testing.assert_array_equal(_continuous_wrist_observations(observed, predicted, measured, span),
                                      [True, True, False, False, True, True, True])
        np.testing.assert_array_equal(_continuous_wrist_observations(observed, observed, measured, span), measured)

    def test_blends_elbow_flips_without_changing_distant_stable_corrections(self):
        delta = np.zeros((24, 2, 3))
        delta[:, 1, 0] = .4
        delta[11, 1, 0] = -1.8
        accepted = np.ones(24, dtype=bool)
        accepted[17] = False
        weights = _bounded_arm_weights(delta, accepted)
        steps = np.linalg.norm(np.diff(delta * weights[:, None, None], axis=0), axis=-1)
        self.assertLessEqual(steps.max(), ARM_UPDATE_STEP + ARM_BLEND_STEP + 1e-9)
        self.assertLess(weights[11], .1)
        self.assertEqual(weights[17], 0)
        np.testing.assert_array_equal(weights[:4], np.ones(4))

    def test_stable_arm_target_still_improves_projection(self):
        length = 12
        poses = np.zeros((length, 52, 3))
        joints = np.zeros_like(poses)
        joints[:, 16] = [0., 0., 3.]
        joints[:, 18] = [.3, 0., 3.]
        joints[:, 20] = [.44, .28, 3.]
        joints[:, 17] = [-.1, 0., 3.]
        joints[:, 19] = [-.4, 0., 3.]
        joints[:, 21] = [-.54, .28, 3.]
        world = np.tile(np.eye(3), (length, 52, 1, 1))
        parents = np.zeros(52, dtype=int)
        parents[18], parents[19] = 16, 17
        points = np.zeros((length, 70, 3))
        target = Rotation.from_rotvec([0., 0., .6]).apply(joints[0, [16, 18, 20]] - joints[0, 16]) + joints[0, 16]
        points[:, [5, 7, 62]] = target
        points[:, [6, 8, 41]] = joints[:, [17, 19, 21]]
        intrinsics = np.tile([[600., 0., 320.], [0., 600., 240.], [0., 0., 1.]], (length, 1, 1))
        support = np.ones(length, dtype=bool)
        camera = SimpleNamespace(K=intrinsics, RT=np.tile(np.eye(4), (length, 1, 1)), camera_valid=support,
                                 image_size_hw=(480, 640))
        result, adjusted, before, after = correct_wrists(poses, joints, world, parents, points, intrinsics,
                                                        camera, support, [{"start": 0, "end": length}])
        self.assertTrue(adjusted[:, 0].all())
        self.assertFalse(adjusted[:, 1].any())
        self.assertTrue((after[:, 0] < before[:, 0] / 2).all())
        other = np.setdiff1d(np.arange(52), [16, 18])
        np.testing.assert_array_equal(result[:, other], poses[:, other])

    def test_limits_wrist_position_jerk_even_below_rotation_limit(self):
        count = 41
        local = np.tile(np.eye(3), (count, 2, 1, 1))
        chain = np.tile([[0., 0., 3.], [.3, 0., 3.], [.6, 0., 3.]], (count, 1, 1))
        delta = np.zeros((count, 2, 3))
        delta[20:, :, 2] = .17
        camera = SimpleNamespace(
            K=np.tile([[600., 0., 320.], [0., 600., 240.], [0., 0., 1.]], (count, 1, 1)),
            RT=np.tile(np.eye(4), (count, 1, 1)),
        )
        target = np.tile([440., 270.], (count, 1))
        error = np.full(count, 30.)
        frames, rotations, residual = _continuous_arm_solution(
            local, delta, np.ones(count, dtype=bool), chain, local, local[:, 0],
            camera, target, error, np.zeros(count, dtype=int), lambda: None,
        )
        wrist = chain[:, 2].copy()
        upper = rotations[:, 0]
        lower = upper @ rotations[:, 1]
        wrist[frames] = chain[frames, 0] + .3 * (upper[:, :, 0] + lower[:, :, 0])
        added = wrist - chain[:, 2]
        jerk = np.linalg.norm(np.diff(added, n=2, axis=0), axis=1)
        self.assertLessEqual(jerk.max(), .6 * WRIST_CORRECTION_ACCEL + 1e-6)
        self.assertLess(residual[-1], 10.)
        np.testing.assert_allclose(added[-5:], np.tile(added[-1], (5, 1)))

if __name__ == "__main__":
    unittest.main()
