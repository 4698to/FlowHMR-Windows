import unittest

import numpy as np

from flowhmr.multishot.stitch_motion import SEAM_FRAMES, fragment_inputs, stitch_fragments


def motion(length, angle, x):
    poses = np.zeros((length,52,3), dtype=np.float32)
    poses[:,18,0] = angle + np.linspace(0,.2,length)
    trans = np.column_stack([np.arange(length)*.02 + x, np.ones(length), np.zeros(length)]).astype(np.float32)
    return dict(poses=poses, trans=trans, betas=np.zeros((1,10), dtype=np.float32),
                mocap_framerate=np.int64(30), gender=np.array('neutral'), body_reference=np.array('neutral'))


class FragmentStitchTest(unittest.TestCase):
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
