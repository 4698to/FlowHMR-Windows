"""GUI crossing-optimisation toggle: parameter plumbing, seed selection, wiring."""
import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import unittest
from unittest.mock import patch

import numpy as np
import torch

from flowhmr import model_files as files
from flowhmr.model_inventory import model_inventory
from flowhmr.pipeline.v2m_inference import _clip_prepared_tracks
from flowhmr.utils.runtime_video_processing import TrackInfo
from flowhmr.utils.sam2_crossing_inputs import (
    CrossingInputsError, _bf16_autocast, check_mask_quality, choose_anchor_frame,
    mask_outage_stats, select_seed_tracks,
)

REPO = Path(__file__).resolve().parents[1]
_SPEC = spec_from_file_location("flowhmr_windows_web_crossing", REPO / "tools/web/server.py")
server = module_from_spec(_SPEC)
_SPEC.loader.exec_module(server)


def track(track_id, first, last, box):
    frames = np.arange(first, last + 1, dtype=np.int64)
    single = np.asarray(box, dtype=np.float32)
    boxes = np.tile(single, (frames.size, 1))
    return TrackInfo(track_id, frames, boxes, length=frames.size,
                     avg_area=float(np.prod(single[2:] - single[:2])))


class SeedSelectionTest(unittest.TestCase):
    def test_keeps_only_people_present_in_the_opening_frames(self):
        seeds = select_seed_tracks([
            track(1, 0, 100, [10, 10, 60, 200]),
            track(2, 3, 90, [100, 10, 150, 200]),
            track(3, 40, 120, [300, 10, 350, 200]),   # enters late
            track(4, 0, 5, [400, 10, 450, 200]),      # too short
        ])
        self.assertEqual([item.track_id for item in seeds], [1, 2])

    def test_rejects_more_people_than_supported(self):
        tracks = [track(index, 0, 30, [index * 80, 0, index * 80 + 60, 200]) for index in range(1, 8)]
        with self.assertRaises(CrossingInputsError):
            select_seed_tracks(tracks, max_people=6)

    def test_requires_a_common_opening_frame(self):
        with self.assertRaises(CrossingInputsError):
            choose_anchor_frame([track(1, 0, 40, [0, 0, 50, 200]),
                                 track(2, 41, 80, [100, 0, 150, 200])])


class AnchorSelectionTest(unittest.TestCase):
    def test_prompt_frame_precedes_the_first_sustained_overlap(self):
        left = track(1, 0, 60, [10, 0, 110, 300])
        right = track(2, 0, 60, [500, 0, 600, 300])
        # From frame 30 on they overlap and stay together.
        left.bboxes[30:] = [100, 0, 200, 300]
        right.bboxes[30:] = [140, 0, 240, 300]
        anchor, seeds, prompts = choose_anchor_frame([left, right])
        self.assertLess(anchor, 30)
        self.assertEqual([item.track_id for item in seeds], [1, 2])
        self.assertLess(prompts[0][0], prompts[1][0])

    def test_single_person_uses_the_first_common_frame(self):
        anchor, seeds, prompts = choose_anchor_frame([track(7, 4, 20, [10, 10, 60, 200])])
        self.assertEqual(anchor, 4)
        self.assertEqual(len(seeds), 1)
        self.assertEqual(prompts, [[10.0, 10.0, 60.0, 200.0]])


class ParameterTest(unittest.TestCase):
    def test_toggle_defaults_to_off(self):
        self.assertFalse(server.default_params()["crossing_opt"])
        self.assertFalse(server.parse_params({})["crossing_opt"])

    def test_toggle_round_trips(self):
        self.assertTrue(server.parse_params({"crossing_opt": True})["crossing_opt"])
        self.assertFalse(server.parse_params({"crossing_opt": False})["crossing_opt"])
        self.assertFalse(server.parse_params({"crossing_opt": "off"})["crossing_opt"])
        self.assertTrue(server.parse_params({}, {"crossing_opt": True})["crossing_opt"])

    def test_old_jobs_without_the_key_stay_off(self):
        restored = {**server.DEFAULT_PARAMS, "hamer": True}
        self.assertFalse(restored["crossing_opt"])


class TrimCompatibilityTest(unittest.TestCase):
    def test_prepared_tracks_are_clipped_to_the_requested_window(self):
        prepared = TrackInfo(7, np.arange(100), np.tile([0, 0, 10, 20], (100, 1)), 100, 200.0,
                             bytetrack_ids=(7, 9), sam2_object_ids=(1,))
        clipped = _clip_prepared_tracks([prepared], 20, 79)
        self.assertEqual(len(clipped), 1)
        track = clipped[0]
        self.assertEqual((int(track.frames.min()), int(track.frames.max())), (20, 79))
        self.assertEqual(track.length, 60)
        self.assertEqual(track.bytetrack_ids, (7, 9))
        self.assertEqual(track.sam2_object_ids, (1,))

    def test_tracks_outside_the_window_are_dropped(self):
        track = TrackInfo(3, np.arange(10), np.tile([0, 0, 10, 20], (10, 1)), 10, 200.0)
        self.assertEqual(_clip_prepared_tracks([track], 50, 80), [])


class CacheReuseTest(unittest.TestCase):
    def test_missing_cache_is_not_reported_ready(self):
        with self.subTest():
            self.assertFalse(server._crossing_inputs_ready(str(Path("missing-cache")), "video.mp4"))


class PrecisionFallbackTest(unittest.TestCase):
    def test_cpu_falls_back_to_plain_fp32(self):
        context = _bf16_autocast(torch.device("cpu"))
        self.assertTrue(hasattr(context, "__enter__"))
        context.__exit__(None, None, None)

    def test_cuda_without_bf16_falls_back(self):
        with patch.object(torch.cuda, "is_bf16_supported", return_value=False):
            context = _bf16_autocast(torch.device("cuda"))
        context.__exit__(None, None, None)


class OptionalModelTest(unittest.TestCase):
    def test_sam2_is_listed_as_an_optional_model(self):
        inventory = model_inventory("cuda", None, None)
        item = next(entry for entry in inventory["items"] if entry["id"] == "sam2")
        self.assertFalse(item["required"])
        self.assertFalse(item["blocking"])
        self.assertEqual(item["folder"], "models/sam2")
        self.assertEqual(files.resolve_model(files.SAM2_CROSSING).name, "sam2.1_hiera_small.pt")
        # Listed in the same catalogue tier as the other optional models.
        ids = [entry["id"] for entry in inventory["items"]]
        self.assertLess(ids.index("flowhmr_base"), ids.index("sam2"))

    def test_availability_tracks_the_checkpoint(self):
        with patch.object(files, "file_present", return_value=False):
            self.assertFalse(files.crossing_optimization_available())
        with patch.object(files, "file_present", return_value=True):
            self.assertTrue(files.crossing_optimization_available())


class MaskQualityTest(unittest.TestCase):
    def _person(self, areas, boxes):
        return np.asarray(areas, dtype=np.int64), np.asarray(boxes, dtype=np.float32)

    def test_outage_run_is_located(self):
        frames = 40
        areas = np.full(frames, 80_000)
        areas[12:34] = 500
        boxes = np.tile([0, 0, 300, 700], (frames, 1))
        stats = mask_outage_stats(areas, boxes)
        self.assertEqual((stats["run_start"], stats["run_end"]), (12, 33))
        self.assertEqual(stats["longest_run"], 22)
        with self.assertRaises(CrossingInputsError):
            check_mask_quality("第 2 个人物", areas, boxes)

    def test_healthy_masks_pass(self):
        frames = 60
        areas = np.full(frames, 90_000)
        boxes = np.tile([0, 0, 300, 700], (frames, 1))
        stats = check_mask_quality("第 1 个人物", areas, boxes)
        self.assertEqual(stats["frames"], 0)

    def test_small_fragment_is_not_an_outage_when_it_matches_the_crop(self):
        boxes = np.tile([0, 0, 60, 60], (10, 1))
        stats = mask_outage_stats(np.full(10, 200), boxes)  # 200px of a 60x60 crop
        self.assertEqual(stats["frames"], 0)

    def test_mask_prompt_score_disables_degenerate_prompts(self):
        from flowhmr.utils.lite_sam_infer import mask_prompt_score

        box = [0, 0, 300, 700]
        self.assertEqual(mask_prompt_score(np.zeros((700, 300), np.uint8), box), 0.0)
        speck = np.zeros((700, 300), np.uint8)
        speck[10:30, 10:30] = 1          # 400 px = 0.19% of the crop
        self.assertEqual(mask_prompt_score(speck, box), 0.0)
        normal = np.zeros((700, 300), np.uint8)
        normal[100:600, 50:250] = 1
        self.assertEqual(mask_prompt_score(normal, box), 1.0)


class CropFollowTest(unittest.TestCase):
    """The prepared crop follows the visible mask (no held position)."""

    reference = [663, 132, 930, 767]
    drifting = [[777, 131, 977, 767], [781, 156, 1034, 795],
                [914, 131, 1126, 740], [1090, 150, 1390, 755]]

    def build(self):
        from flowhmr.utils.sam2_body_boxes import build_body_boxes

        masks = np.asarray([self.reference] * 5 + self.drifting, dtype=np.float32)
        reliable = [True] * 5 + [False] * len(self.drifting)
        return masks, build_body_boxes(masks, reliable)

    def test_body_scale_frames_track_the_visible_mask(self):
        masks, policy = self.build()
        for index, mask in enumerate(self.drifting):
            frame = 5 + index
            mask = np.asarray(mask, dtype=np.float32)
            body = policy.body_boxes[frame]
            height = body[3] - body[1]
            width = body[2] - body[0]
            self.assertLessEqual(width, height * 1.05 + 1)
            self.assertLessEqual(body[0], mask[0] + 1)
            self.assertGreaterEqual(body[2], mask[2] - 1)
            self.assertLess(abs((body[0] + body[2]) / 2 - (mask[0] + mask[2]) / 2), 60)
        # Vertical extent is trusted and never clamped.
        self.assertLessEqual(policy.body_boxes[6, 1], min(self.reference[1], self.drifting[1][1]))
        self.assertGreaterEqual(policy.body_boxes[6, 3], self.drifting[1][3] - 1)

    def test_visible_frames_use_the_full_body_observation(self):
        masks, policy = self.build()
        for frame in range(5):
            np.testing.assert_array_equal(policy.body_boxes[frame], masks[frame])
            self.assertEqual(policy.states[frame], "visible")


class UiWiringTest(unittest.TestCase):
    def test_toggle_and_labels_exist(self):
        html = (REPO / "tools/web/index.html").read_text(encoding="utf-8")
        self.assertIn('id="optCrossing"', html)
        self.assertIn("crossing.title", html)
        self.assertIn("crossing.hint", html)
        script = (REPO / "tools/web/static/js/params.js").read_text(encoding="utf-8")
        self.assertIn("optCrossing", script)
        self.assertIn("crossing_opt", script)
        person = (REPO / "tools/web/static/js/person-select.js").read_text(encoding="utf-8")
        self.assertIn("person.merged_ids", person)
        main = (REPO / "tools/web/static/js/main.js").read_text(encoding="utf-8")
        self.assertIn("crossing_available", main)
        for code in ("zh-CN", "zh-TW", "en", "ja", "ko", "ru", "fr"):
            text = (REPO / f"tools/web/static/js/locales/{code}.js").read_text(encoding="utf-8")
            self.assertIn('"crossing.title"', text, code)
            self.assertIn('"crossing.hint"', text, code)
            self.assertIn('"crossing.tooltip"', text, code)
            self.assertIn('"crossing.missing"', text, code)
            self.assertIn('"person.merged_ids"', text, code)


if __name__ == "__main__":
    unittest.main()
