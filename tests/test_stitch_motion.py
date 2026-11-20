import unittest

import numpy as np
from scipy.spatial.transform import Rotation

from flowhmr.multishot.motion_condition import joined_motion_camera
from flowhmr.multishot.stitch_motion import (
    SEAM_FRAMES, _boxes_for_shot_timeline, _continuous_generation_parts, _include_verified_edge_frames,
    fragment_inputs, stitch_fragments,
)


def motion(length, angle, x):
    poses = np.zeros((length,52,3), dtype=np.float32)
    poses[:,18,0] = angle + np.linspace(0,.2,length)
    trans = np.column_stack([np.arange(length)*.02 + x, np.ones(length), np.zeros(length)]).astype(np.float32)
    return dict(poses=poses, trans=trans, betas=np.zeros((1,10), dtype=np.float32),
                mocap_framerate=np.int64(30), gender=np.array('neutral'), body_reference=np.array('neutral'))


class FragmentStitchTest(unittest.TestCase):
    def test_cut_uses_heading_at_same_time_on_both_sides(self):
        rt = np.broadcast_to(np.eye(4), (24,4,4)).copy()
        ahead = np.arange(24)*.06
        ahead[12:] += .7
        orientation = Rotation.from_rotvec(ahead[:,None]*[0,1,0]).as_matrix()
        valid = np.ones(24,bool)
        shots = [dict(start=0,end=12), dict(start=12,end=24)]
        conditioned = joined_motion_camera(rt, orientation, valid, valid, shots, fit_cut_velocity=True)
        np.testing.assert_allclose(conditioned[12,:3,:3],
                                   Rotation.from_rotvec([0,.7,0]).as_matrix(), atol=1e-12)
        np.testing.assert_array_equal(conditioned[:12], rt[:12])

    def test_missing_box_at_cut_uses_the_incoming_shot(self):
        boxes = np.array([[0,0,10,20], [0,0,10,20], [40,0,60,20], [80,0,110,20], [82,0,112,20]], np.float32)
        revised, supported = _boxes_for_shot_timeline(10, boxes, np.array([10,11,13,14]), [(10,12), (12,15)])
        np.testing.assert_array_equal(revised[2], boxes[3])
        np.testing.assert_array_equal(revised[[0,1,3,4]], boxes[[0,1,3,4]])
        self.assertTrue(supported.all())

    def test_shared_generation_requires_contiguous_frames_and_two_observed_arms(self):
        parts = [(a, b, owner, np.arange(a, b+1), np.full((b-a+1, 4), owner, np.float32))
                 for a, b, owner in ((0, 19, 1), (20, 39, 2), (41, 59, 3))]
        self.assertEqual(len(_continuous_generation_parts(parts, {20: (0,), 41: (0, 1)}, 100)), 3)
        self.assertEqual(len(_continuous_generation_parts(parts, {20: (0, 1)}, 39)), 3)
        joined = _continuous_generation_parts(parts, {20: (0, 1), 41: (0, 1)}, 100)
        self.assertEqual([part[:3] for part in joined], [(0, 39, 1), (41, 59, 3)])
        np.testing.assert_array_equal(joined[0][3], np.arange(40))
        np.testing.assert_array_equal(joined[0][4][:, 0], np.repeat([1, 2], 20))

    def test_verified_boundary_reuses_box_without_changing_detector_evidence(self):
        left_boxes = np.arange(12, dtype=np.float32).reshape(3, 4)
        right_boxes = left_boxes + 20
        left_seen, right_seen = np.arange(10, 13), np.arange(15, 18)
        parts = [(10, 12, 1, left_seen, left_boxes), (15, 17, 2, right_seen, right_boxes)]
        left, right = _include_verified_edge_frames(parts, {13: 12, 14: 15, 18: 17, 20: 17})
        self.assertEqual(left[:3], (10, 13, 1))
        self.assertEqual(right[:3], (14, 18, 2))
        np.testing.assert_array_equal(left[3], left_seen)
        np.testing.assert_array_equal(right[3], right_seen)
        np.testing.assert_array_equal(left[4][-1], left_boxes[-1])
        np.testing.assert_array_equal(right[4][[0, -1]], right_boxes[[0, -1]])

    def test_only_handoff_changes_local_body_motion(self):
        left = motion(22, .2, 1.)
        right = motion(19, .55, 5.)
        right['poses'][:,0,1] = 1.3
        joined, report = stitch_fragments([(13,34,left),(37,55,right)],13,55,
                                          references={35:34,36:37})
        self.assertEqual(report['fragments'], [[13,35],[37,56]])
        self.assertEqual(report['seams'], [[35-SEAM_FRAMES,37+SEAM_FRAMES]])
        a,b = report['seams'][0]
        np.testing.assert_array_equal(joined['poses'][:a-13], left['poses'][:a-13])
        np.testing.assert_array_equal(joined['trans'][:a-13], left['trans'][:a-13])
        np.testing.assert_array_equal(joined['poses'][b-13:,1:], right['poses'][b-37:,1:])
        self.assertTrue(np.isfinite(joined['poses']).all())
        self.assertTrue(np.all(np.diff(joined['poses'][a-14:b-12,18,0]) >= 0))
        np.testing.assert_allclose(joined['trans'][b-13] - joined['trans'][a-14],
                                   [.02*(38-a+(b-37)*np.cos(1.3)),0,.02*(b-37)*np.sin(1.3)], atol=1e-6)

    def test_hard_cut_keeps_both_native_poses_and_uses_same_shot_dropout(self):
        left = motion(22, .2, 1.)
        right = motion(19, .55, 5.)
        joined, report = stitch_fragments([(13,34,left),(36,54,right)],13,54,
                                          cuts=[35], references={35:36})
        self.assertEqual(report['seams'], [])
        self.assertEqual(report['handoffs'], [dict(source_start=36, cut=35, blended=False)])
        np.testing.assert_array_equal(joined['poses'][:22,1:], left['poses'][:,1:])
        np.testing.assert_array_equal(joined['poses'][23:,1:], right['poses'][:,1:])
        np.testing.assert_array_equal(joined['poses'][22], joined['poses'][23])
        self.assertNotIn('valid_frames', joined)

    def test_different_articulation_does_not_fabricate_a_visible_transition(self):
        left = motion(22, .2, 1.)
        right = motion(19, 1.5, 5.)
        joined, report = stitch_fragments([(13,34,left),(38,56,right)],13,56)
        self.assertEqual(report['seams'], [])
        self.assertEqual(report['visible_ranges'], [[13,35],[38,57]])
        np.testing.assert_array_equal(joined['poses'][22:25],
                                      np.broadcast_to(left['poses'][-1], (3,52,3)))
        self.assertFalse(joined['valid_frames'][22:25].any())
        np.testing.assert_array_equal(joined['poses'][25:,1:],right['poses'][:,1:])

    def test_supported_cut_softens_just_the_observed_arm(self):
        left = motion(22, .2, 1.)
        right = motion(19, 1.5, 5.)
        parts = [(13,34,left),(36,54,right)]
        plain, _ = stitch_fragments(parts,13,54,cuts=[35],references={35:36})
        joined, report = stitch_fragments(parts,13,54,cuts=[35],references={35:36},
                                          observed_arms={35:(0,)})
        self.assertEqual(report['handoffs'][0]['arm_transition'], ['left'])
        self.assertAlmostEqual(float(np.degrees(joined['poses'][22,18,0]-joined['poses'][21,18,0])),12.,places=4)
        np.testing.assert_array_equal(joined['trans'], plain['trans'])
        other = np.setdiff1d(np.arange(52), [16,18,20])
        np.testing.assert_array_equal(joined['poses'][:,other], plain['poses'][:,other])
        np.testing.assert_array_equal(joined['poses'][:18], plain['poses'][:18])
        np.testing.assert_array_equal(joined['poses'][27:], plain['poses'][27:])

    def test_interleaved_tracks_keep_owner_and_cut_bboxes_separate(self):
        frames = np.array([3,4,15,17,8,9,10])
        owners = np.array([1,1,1,1,2,2,2])
        boxes = np.column_stack([owners*20, frames, owners*20+10, frames+50]).astype(np.float32)
        parts = fragment_inputs(dict(fragment_frames=frames, fragment_boxes=boxes, fragment_ids=owners),
                                3,17,100,100)
        self.assertEqual([part[:3] for part in parts], [(3,4,1),(8,10,2),(15,17,1)])
        np.testing.assert_array_equal(parts[-1][4][:,1], [15,16,17])
        np.testing.assert_array_equal(parts[1][4][:,0], [40,40,40])


if __name__ == '__main__':
    unittest.main()
