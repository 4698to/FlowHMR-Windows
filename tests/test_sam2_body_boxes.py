"""Body crop policy for the experimental crossing optimisation.

No held crop: every frame follows the current visible mask; a body-scale prior
comes from the last reliable full-body observation and the width is bounded by
the arm-span height prior.
"""
import unittest

import numpy as np

from flowhmr.utils.sam2_body_boxes import (
    ARM_SPAN_RATIO, BODY_POLICY_VERSION, build_body_boxes,
)


def boxes(*rows):
    return np.asarray(rows, dtype=np.float32)


def contains(outer, inner):
    return np.all(outer[:2] <= inner[:2]) and np.all(outer[2:] >= inner[2:])


class VisibleFrameTest(unittest.TestCase):
    def test_reliable_frame_unions_mask_and_detector(self):
        observed = boxes([5, 5, 30, 90])
        detector = boxes([0, 0, 25, 100])
        result = build_body_boxes(observed, [True], detector)
        np.testing.assert_array_equal(result.body_boxes[0], [0, 0, 30, 100])
        self.assertEqual(result.states[0], "visible")

    def test_missing_mask_cannot_claim_reliable_visibility(self):
        observed = boxes([0, 0, 0, 0], [0, 0, 40, 100], [0, 0, 40, 100])
        result = build_body_boxes(observed, [True, True, True])
        self.assertEqual(result.states.tolist(), ["not_ready", "visible", "visible"])

    def test_not_ready_prefix_is_marked_without_backfill(self):
        observed = boxes([0, 0, 0, 0], [20, 90, 21, 91], [0, 0, 40, 100])
        result = build_body_boxes(observed, [False, False, True])
        self.assertEqual(result.states.tolist(), ["not_ready", "not_ready", "visible"])
        self.assertEqual(result.ready.tolist(), [False, False, True])
        self.assertTrue(np.isnan(result.body_boxes[0]).all())
        with self.assertRaisesRegex(ValueError, "no reliable initial"):
            build_body_boxes(observed, [False, False, True], uninitialized="raise")

    def test_detector_is_ignored_on_unreliable_frames_with_masks(self):
        observed = boxes([0, 0, 40, 100], [10, 20, 30, 90])
        detector = boxes([0, 0, 40, 100], [-1000, -1000, 1000, 1000])
        result = build_body_boxes(observed, [True, False], detector)
        self.assertGreaterEqual(result.body_boxes[1, 0], -0.5)
        self.assertLessEqual(result.body_boxes[1, 2], 40.5)


class FollowingFrameTest(unittest.TestCase):
    reference = [663, 132, 930, 767]     # 267 wide, 635 tall

    def build(self, masks):
        observed = boxes(*[self.reference] * 5, *masks)
        return build_body_boxes(observed, [True] * 5 + [False] * len(masks))

    def test_partial_mask_centres_the_body_scale_crop_on_itself(self):
        masks = [[777, 131, 977, 767], [914, 131, 1126, 740], [954, 150, 1253, 755]]
        result = self.build(masks)
        for index, mask in enumerate(masks):
            frame = 5 + index
            mask = np.asarray(mask, dtype=np.float32)
            body = result.body_boxes[frame]
            self.assertTrue(contains(body, mask))
            self.assertEqual(result.states[frame], "estimated")
            # Crop centre stays close to the visible support, not the old anchor.
            self.assertLess(abs((body[0] + body[2]) / 2 - (mask[0] + mask[2]) / 2), 60)

    def test_vertical_extent_is_never_clamped(self):
        masks = [[770, 60, 830, 900]]
        result = self.build(masks)
        mask = np.asarray(masks[0], dtype=np.float32)
        self.assertTrue(contains(result.body_boxes[5], mask))

    def test_body_scale_width_comes_from_the_reference(self):
        result = self.build([[660, 150, 700, 750]])
        width = result.body_boxes[5, 2] - result.body_boxes[5, 0]
        height = result.body_boxes[5, 3] - result.body_boxes[5, 1]
        self.assertGreaterEqual(width, 267 - 1)
        self.assertLessEqual(width, height * ARM_SPAN_RATIO + 1)


class ArmSpanPriorTest(unittest.TestCase):
    # An implausibly wide reference (e.g. a merged detector box) is the case the
    # prior exists for: the crop keeps the current evidence side and drops the
    # stale one instead of inheriting the full width.
    reference = [300, 132, 1000, 767]    # 700 wide, 635 tall
    masks = [[900, 150, 1200, 755], [950, 150, 1250, 755]]

    def build(self):
        observed = boxes(*[self.reference] * 5, *self.masks)
        return build_body_boxes(observed, [True] * 5 + [False] * len(self.masks))

    def test_one_sided_width_beyond_the_prior_pulls_the_stale_side_in(self):
        with self.assertLogs("flowhmr.utils.sam2_body_boxes", level="WARNING") as logs:
            result = self.build()
        self.assertTrue(any("arm-span prior clamped" in line for line in logs.output))
        for frame in (5, 6):
            mask = np.asarray(self.masks[frame - 5], dtype=np.float32)
            body = result.body_boxes[frame]
            height = body[3] - body[1]
            width = body[2] - body[0]
            self.assertLessEqual(width, height * ARM_SPAN_RATIO + 1)
            self.assertTrue(contains(body, mask))
            self.assertGreater(body[0], self.reference[0])

    def test_two_sided_expansion_keeps_the_evidence(self):
        masks = [[500, 132, 1300, 767]]
        observed = boxes(*[self.reference] * 5, *masks)
        result = build_body_boxes(observed, [True] * 5 + [False])
        width = result.body_boxes[5, 2] - result.body_boxes[5, 0]
        height = result.body_boxes[5, 3] - result.body_boxes[5, 1]
        self.assertGreater(width, height * ARM_SPAN_RATIO - 1)
        mask = np.asarray(masks[0], dtype=np.float32)
        self.assertTrue(contains(result.body_boxes[5], mask))


class FallbackTest(unittest.TestCase):
    def test_detector_is_used_when_the_mask_is_missing(self):
        observed = boxes([0, 0, 40, 100], [0, 0, 0, 0], [np.nan] * 4)
        detector = boxes([5, 5, 30, 90], [12, 8, 33, 92], [0, 0, 0, 0])
        result = build_body_boxes(observed, [True, False, False], detector)
        np.testing.assert_array_equal(result.body_boxes[1], detector[1])
        self.assertEqual(result.states[1], "detector")

    def test_previous_crop_is_kept_when_nothing_is_available(self):
        observed = boxes([0, 0, 40, 100], [0, 0, 0, 0], [0, 0, 0, 0])
        result = build_body_boxes(observed, [True, False, False])
        np.testing.assert_array_equal(result.body_boxes[1], result.body_boxes[0])
        np.testing.assert_array_equal(result.body_boxes[2], result.body_boxes[0])
        self.assertEqual(result.states.tolist(), ["visible", "fallback", "fallback"])


class ContractTest(unittest.TestCase):
    def test_policy_version_is_exposed_for_cache_validation(self):
        self.assertIsInstance(BODY_POLICY_VERSION, str)
        self.assertTrue(BODY_POLICY_VERSION)

    def test_prefix_invariance(self):
        observed = boxes([0, 0, 20, 40], [1, 1, 21, 41], [5, 30, 7, 31],
                         [2, -20, 18, 42], [0, 0, 0, 0], [0, 0, 20, 40])
        reliable = np.array([True, True, False, False, False, True])
        full = build_body_boxes(observed, reliable)
        for stop in range(1, len(observed) + 1):
            prefix = build_body_boxes(observed[:stop], reliable[:stop])
            np.testing.assert_array_equal(prefix.body_boxes, full.body_boxes[:stop])
            np.testing.assert_array_equal(prefix.states, full.states[:stop])
            np.testing.assert_array_equal(prefix.ready, full.ready[:stop])
            self.assertEqual(prefix.reference_frames, full.reference_frames[:stop])

    def test_inputs_are_not_mutated_and_empty_input_works(self):
        observed = boxes([0, 0, 40, 100], [10, 50, 20, 80])
        detector = observed.copy()
        saved = observed.copy()
        build_body_boxes(observed, np.array([True, False]), detector)
        np.testing.assert_array_equal(saved, observed)
        np.testing.assert_array_equal(saved, detector)
        result = build_body_boxes(np.empty((0, 4)), np.empty(0, dtype=bool))
        self.assertEqual(result.body_boxes.shape, (0, 4))
        self.assertEqual(result.reference_frames, ())

    def test_argument_validation(self):
        observed = boxes([0, 0, 40, 100])
        for kwargs in [{"uninitialized": "future"}]:
            with self.assertRaises(ValueError):
                build_body_boxes(observed, [True], **kwargs)
        for flags in [[True, False], [np.nan], [[True]]]:
            with self.assertRaises(ValueError):
                build_body_boxes(observed, flags)
        with self.assertRaises(ValueError):
            build_body_boxes([0, 0, 40, 100], [True])
        with self.assertRaises(ValueError):
            build_body_boxes(observed, [True], np.zeros((2, 4)))


if __name__ == "__main__":
    unittest.main()
