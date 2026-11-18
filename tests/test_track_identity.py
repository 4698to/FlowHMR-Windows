"""Identity merging across ByteTrack ID switches, constrained by SAM2 continuity."""

import unittest

import numpy as np

from flowhmr.utils.runtime_video_processing import TrackInfo, TrackingResult
from flowhmr.utils.track_identity import Sam2Object, merge_result_identities, merge_sam2_identities


def track(track_id, first, last, box):
    frames = np.arange(first, last + 1, dtype=np.int64)
    boxes = np.tile(np.asarray(box, dtype=np.float32), (frames.size, 1))
    return TrackInfo(track_id, frames, boxes)


def boxes_for(frames, box):
    return np.tile(np.asarray(box, dtype=np.float32), (len(frames), 1))


class IdentityMergeTest(unittest.TestCase):
    def setUp(self):
        self.frames = np.arange(0, 30, dtype=np.int64)
        self.target_boxes = boxes_for(self.frames, [10, 10, 30, 50])
        self.target = Sam2Object(1, self.frames, self.target_boxes)
        self.other = Sam2Object(2, self.frames, boxes_for(self.frames, [100, 10, 120, 50]))

    def test_merges_two_fragments_that_one_sam2_object_covers(self):
        before = track(11, 0, 9, [10, 10, 30, 50])
        after = track(12, 20, 29, [10, 10, 30, 50])
        result = merge_sam2_identities([before, after], [self.target])
        self.assertEqual(len(result.tracks), 1)
        merged = result.tracks[0]
        self.assertEqual(merged.track_id, 11)
        self.assertEqual(merged.bytetrack_ids, (11, 12))
        self.assertEqual(merged.sam2_object_ids, (1,))
        self.assertEqual(result.merges[0].start, 0)
        self.assertEqual(result.merges[0].end, 29)
        self.assertEqual(result.merges[0].bytetrack_ids, (11, 12))

    def test_does_not_merge_when_sam2_misses_part_of_the_gap(self):
        gap = np.array([frame for frame in self.frames if frame != 15], dtype=np.int64)
        target = Sam2Object(1, gap, self.target_boxes[gap])
        result = merge_sam2_identities(
            [track(11, 0, 9, [10, 10, 30, 50]), track(12, 20, 29, [10, 10, 30, 50])], [target])
        self.assertEqual(len(result.tracks), 2)
        self.assertFalse(result.merges)

    def test_does_not_merge_different_sam2_objects(self):
        result = merge_sam2_identities(
            [track(11, 0, 9, [10, 10, 30, 50]), track(12, 20, 29, [100, 10, 120, 50])],
            [self.target, self.other])
        self.assertEqual(len(result.tracks), 2)
        self.assertFalse(result.merges)

    def test_rejects_ambiguous_object_assignment(self):
        duplicate = Sam2Object(3, self.frames, self.target_boxes)
        result = merge_sam2_identities(
            [track(11, 0, 9, [10, 10, 30, 50]), track(12, 20, 29, [10, 10, 30, 50])],
            [self.target, duplicate])
        self.assertEqual(len(result.tracks), 2)
        self.assertFalse(result.merges)
        self.assertIn(11, result.ambiguous)

    def test_does_not_merge_overlapping_fragments(self):
        result = merge_sam2_identities(
            [track(11, 0, 14, [10, 10, 30, 50]), track(12, 10, 29, [10, 10, 30, 50])],
            [self.target])
        self.assertEqual(len(result.tracks), 2)
        self.assertFalse(result.merges)

    def test_tracks_without_sam2_objects_keep_original_ids(self):
        before = track(11, 0, 9, [10, 10, 30, 50])
        resolved = merge_sam2_identities([before], [])
        self.assertEqual(resolved.tracks[0].track_id, 11)
        self.assertEqual(resolved.tracks[0].bytetrack_ids, (11,))

    def test_mask_only_object_derives_boxes(self):
        height, width = 60, 40
        masks = np.zeros((len(self.frames), height, width), dtype=np.uint8)
        masks[:, 10:50, 10:30] = 1
        target = Sam2Object(1, self.frames, masks=masks)
        result = merge_sam2_identities(
            [track(11, 0, 9, [10, 10, 30, 50]), track(12, 20, 29, [10, 10, 30, 50])], [target])
        self.assertEqual(len(result.tracks), 1)
        self.assertEqual(result.tracks[0].bytetrack_ids, (11, 12))

    def test_merge_result_keeps_video_metadata(self):
        result = TrackingResult([track(11, 0, 9, [10, 10, 30, 50]),
                                 track(12, 20, 29, [10, 10, 30, 50])], 30, 320, 240, (0, 29))
        merged = merge_result_identities(result, [self.target])
        self.assertEqual((merged.total_frames, merged.width, merged.height, merged.scanned_range),
                         (30, 320, 240, (0, 29)))
        self.assertEqual([item.track_id for item in merged.tracks], [11])
    def test_tiny_contained_mask_is_not_full_body_identity_evidence(self):
        masks = np.zeros((30, 60, 40), np.uint8)
        masks[:, 20:23, 15:18] = 1
        tiny = Sam2Object(1, self.frames, masks=masks)
        result = merge_sam2_identities(
            [track(11, 0, 9, [10, 10, 30, 50]), track(12, 20, 29, [10, 10, 30, 50])], [tiny])
        self.assertEqual(len(result.tracks), 2)
        self.assertFalse(result.merges)

    def test_visible_false_cannot_supply_identity_anchors(self):
        target = Sam2Object(1, self.frames, self.target_boxes, visible=np.zeros(30, bool))
        result = merge_sam2_identities(
            [track(11, 0, 9, [10, 10, 30, 50]), track(12, 20, 29, [10, 10, 30, 50])], [target])
        self.assertFalse(result.merges)
        self.assertTrue(all(not t.sam2_object_ids for t in result.tracks))

    def test_sparse_hits_do_not_count_as_consecutive_visible_anchors(self):
        before = track(11, 0, 8, [10, 10, 30, 50])
        after = track(12, 20, 28, [10, 10, 30, 50])
        before.frames = before.frames[::2]
        before.bboxes = before.bboxes[::2]
        after.frames = after.frames[::2]
        after.bboxes = after.bboxes[::2]
        result = merge_sam2_identities([before, after], [self.target])
        self.assertFalse(result.merges)

    def test_earliest_identity_is_time_order_not_smallest_integer(self):
        result = merge_sam2_identities(
            [track(2, 20, 29, [10, 10, 30, 50]), track(99, 0, 9, [10, 10, 30, 50])],
            [self.target])
        self.assertEqual(result.tracks[0].track_id, 99)
        self.assertEqual(result.tracks[0].bytetrack_ids, (99, 2))

    def test_three_way_overlap_cannot_be_bypassed_by_chain_splitting(self):
        # A overlaps B, then B ends before C. Neither A+C nor B+C is safe:
        # they would discard a second simultaneous actor's identity evidence.
        result = merge_sam2_identities(
            [track(11, 0, 14, [10, 10, 30, 50]), track(12, 10, 19, [10, 10, 30, 50]),
             track(13, 20, 29, [10, 10, 30, 50])], [self.target])
        self.assertFalse(result.merges)
        self.assertEqual(len(result.tracks), 3)
        self.assertIn(12, result.ambiguous)

    def test_empty_masks_preserve_gap_continuity_without_matching_anchors(self):
        masks = np.zeros((30, 60, 40), np.uint8)
        masks[:10, 10:50, 10:30] = 1
        masks[20:, 10:50, 10:30] = 1
        target = Sam2Object(1, self.frames, masks=masks)
        result = merge_sam2_identities(
            [track(11, 0, 9, [10, 10, 30, 50]), track(12, 20, 29, [10, 10, 30, 50])], [target])
        self.assertEqual(len(result.tracks), 1)
        # The gap was not invented as extra detector observations.
        np.testing.assert_array_equal(result.tracks[0].frames, np.r_[0:10, 20:30])

    def test_mask_evidence_wins_over_supplied_held_full_body_box(self):
        masks = np.zeros((30, 60, 40), np.uint8)
        masks[:, 20:22, 15:17] = 1
        target = Sam2Object(1, self.frames, boxes=self.target_boxes, masks=masks)
        result = merge_sam2_identities(
            [track(11, 0, 9, [10, 10, 30, 50]), track(12, 20, 29, [10, 10, 30, 50])], [target])
        self.assertFalse(result.merges)

    def test_different_objects_on_different_runs_veto_fragment_identity(self):
        a = self.target_boxes.copy()
        b = boxes_for(self.frames, [100, 10, 120, 50])
        a[5:10], b[5:10] = b[5:10].copy(), a[5:10].copy()
        result = merge_sam2_identities(
            [track(11, 0, 9, [10, 10, 30, 50]), track(12, 20, 29, [10, 10, 30, 50])],
            [Sam2Object(1, self.frames, a), Sam2Object(2, self.frames, b)])
        self.assertFalse(result.merges)
        self.assertIn(11, result.ambiguous)

    def test_sources_are_never_mutated_including_unmerged_tracks(self):
        before, after = track(11, 0, 9, [10, 10, 30, 50]), track(12, 20, 29, [10, 10, 30, 50])
        before.bytetrack_ids = (101, 11)
        before.sam2_object_ids = (77,)
        other = track(88, 0, 29, [100, 10, 120, 50])
        arrays = [(t.frames.copy(), t.bboxes.copy(), t.bytetrack_ids, t.sam2_object_ids)
                  for t in (before, after, other)]
        result = merge_sam2_identities([before, after, other], [self.target, self.other])
        for t, snapshot in zip((before, after, other), arrays):
            np.testing.assert_array_equal(t.frames, snapshot[0])
            np.testing.assert_array_equal(t.bboxes, snapshot[1])
            self.assertEqual((t.bytetrack_ids, t.sam2_object_ids), snapshot[2:])
        merged = next(t for t in result.tracks if t.track_id == 11)
        self.assertEqual(merged.bytetrack_ids, (101, 11, 12))
        self.assertEqual(merged.sam2_object_ids, (77, 1))
        for t in result.tracks:
            t.bboxes[:] = 0
        self.assertTrue(np.any(other.bboxes))
        self.assertTrue(np.any(before.bboxes))
        unconditioned = merge_sam2_identities([other], [])
        unconditioned.tracks[0].bboxes[:] = 0
        self.assertTrue(np.any(other.bboxes))

    def test_mask_pixel_support_rejects_disconnected_bbox_false_match(self):
        # Two disjoint silhouettes can have the same enclosing bbox; little of
        # the actual mask lies inside this detector box, so it is no anchor.
        masks = np.zeros((30, 100, 100), np.uint8)
        masks[:, :10, :10] = 1
        masks[:, 90:, 90:] = 1
        obj = Sam2Object(1, self.frames, masks=masks)
        result = merge_sam2_identities(
            [track(11, 0, 9, [20, 20, 80, 80]), track(12, 20, 29, [20, 20, 80, 80])], [obj])
        self.assertFalse(result.merges)

    def test_mixed_actor_track_cannot_poison_an_independent_clean_merge(self):
        # ID 88 visibly switches people, so it is not evidence that two
        # consistent IDs 11/12 represent different simultaneous people.
        mixed = track(88, 0, 19, [10, 10, 30, 50])
        a = track(11, 0, 9, [100, 10, 120, 50])
        b = track(12, 20, 29, [100, 10, 120, 50])
        # Give mixed its own unobstructed target-B evidence between a/b.
        mixed.bboxes[10:15] = [100, 10, 120, 50]
        mixed.bboxes[15:20] = [10, 10, 30, 50]
        resolved = merge_sam2_identities([mixed, a, b], [self.target, self.other])
        self.assertIn(88, resolved.ambiguous)
        self.assertEqual([m.bytetrack_ids for m in resolved.merges], [(11, 12)])
        self.assertEqual(next(t for t in resolved.tracks if t.track_id == 88).sam2_object_ids, ())

    def test_visibility_list_is_normalized_and_empty_mask_overrides_boxes(self):
        masks = np.zeros((30, 60, 40), np.uint8)
        obj = Sam2Object(1, list(range(30)), boxes=self.target_boxes.tolist(), masks=masks,
                         visible=[True] * 30)
        self.assertFalse(obj.visible_at(0))
        result = merge_sam2_identities(
            [track(11, 0, 9, [10, 10, 30, 50]), track(12, 20, 29, [10, 10, 30, 50])], [obj])
        self.assertFalse(result.merges)


if __name__ == "__main__":
    unittest.main()
