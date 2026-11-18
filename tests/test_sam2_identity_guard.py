"""CPU-only guard for duplicated SAM2 labels; masks are never modified."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import numpy as np

from flowhmr.utils.sam2_identity_guard import (
    IdentityGuardSettings, Sam2IdentityCollisionError,
    find_identity_collisions, validate_identity_masks,
)
from flowhmr.utils.sam2_person_inputs import PackedMasks, file_sha256, load_person_inputs


def inputs(*masks):
    people, streams = [], {}
    for cid, mask in enumerate(masks, 1):
        total, height, width = mask.shape
        packed = np.packbits(mask.reshape(total, -1), axis=1)
        streams[cid] = PackedMasks(packed, height, width)
        people.append(dict(canonical_id=cid, object_id=cid + 10, frame_range=[0, total - 1]))
    return people, streams


class IdentityGuardTest(unittest.TestCase):
    def test_sustained_clone_reports_ids_segments_and_preserves_pixels(self):
        mask = np.zeros((12, 10, 12), np.uint8)
        mask[:, 2:8, 3:9] = 1
        second = np.zeros_like(mask)
        second[2:6] = mask[2:6]
        second[8:11] = mask[8:11]
        people, streams = inputs(mask, second)
        before = [s.packed.copy() for s in streams.values()]
        collisions = find_identity_collisions(people, streams)
        self.assertEqual(collisions[0]['segments'], [[2, 5], [8, 10]])
        self.assertEqual(collisions[0]['object_ids'], [11, 12])
        with self.assertRaisesRegex(Sam2IdentityCollisionError, 'S11/S12.*2–5, 8–10'):
            validate_identity_masks(people, streams, {1: 'a'*64, 2: 'b'*64})
        for stream, original in zip(streams.values(), before):
            np.testing.assert_array_equal(stream.packed, original)

    def test_isolated_overlap_and_short_two_frame_overlap_allowed(self):
        mask = np.ones((8, 10, 10), np.uint8)
        second = np.zeros_like(mask)
        second[[0, 2, 3, 6]] = mask[[0, 2, 3, 6]]
        self.assertEqual(find_identity_collisions(*inputs(mask, second)), [])

    def test_empty_occlusion_breaks_clone_run(self):
        mask = np.ones((5, 10, 10), np.uint8)
        second = mask.copy()
        second[2] = 0
        people, streams = inputs(mask, second)
        self.assertEqual(find_identity_collisions(people, streams), [])
        self.assertEqual(streams[2][2].sum(), 0)

    def test_small_visible_fragment_inside_other_body_is_retained(self):
        whole = np.ones((6, 10, 10), np.uint8)
        tiny = np.zeros_like(whole)
        tiny[:, 4, 4] = 1
        people, streams = inputs(whole, tiny)
        self.assertEqual(find_identity_collisions(people, streams), [])
        self.assertEqual(streams[2][:].sum(), 6)

    def test_distinct_partial_masks_and_zero_evidence_are_allowed(self):
        first = np.zeros((7, 10, 10), np.uint8)
        second = first.copy()
        first[:, :6, :6] = 1
        second[:, 4:, 4:] = 1
        self.assertEqual(find_identity_collisions(*inputs(first, second, np.zeros_like(first))), [])

    def test_same_tiny_fragment_is_rejected_only_when_sustained(self):
        tiny = np.zeros((3, 10, 10), np.uint8)
        tiny[:, 4, 4] = 1
        people, streams = inputs(tiny, tiny)
        self.assertEqual(find_identity_collisions(people, streams)[0]['segments'], [[0, 2]])
        self.assertEqual(streams[1][:].sum(), 3)

    def test_inactive_frames_break_evidence(self):
        full = np.ones((8, 10, 10), np.uint8)
        people, streams = inputs(full, full)
        people[0]['frame_range'] = [0, 3]
        people[1]['frame_range'] = [2, 7]
        self.assertEqual(find_identity_collisions(people, streams), [])

    def test_padding_bits_are_not_visible_pixels(self):
        masks = np.zeros((3, 1, 3), np.uint8)
        people, streams = inputs(masks, masks)
        streams[1].packed[:] = 31
        streams[2].packed[:] = 31
        before = streams[1].packed.copy()
        self.assertEqual(find_identity_collisions(people, streams), [])
        np.testing.assert_array_equal(before, streams[1].packed)

    def test_certificate_reuses_actual_hash_and_invalidates_on_settings_or_bytes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'identity_guard.json'
            first = np.zeros((4, 10, 10), np.uint8)
            second = first.copy()
            first[:, :4, :4] = 1
            second[:, 6:, 6:] = 1
            people, streams = inputs(first, second)
            hashes = {1: 'a'*64, 2: 'b'*64}
            certificate = validate_identity_masks(people, streams, hashes, cache_path=path)
            with patch('flowhmr.utils.sam2_identity_guard.find_identity_collisions', side_effect=AssertionError('scan reused')):
                reused = validate_identity_masks(people, streams, hashes, cache_path=path)
            self.assertEqual(certificate['cache_key'], reused['cache_key'])
            changed = validate_identity_masks(people, streams, hashes, cache_path=path,
                                             settings=IdentityGuardSettings(consecutive_frames=4))
            self.assertNotEqual(changed['cache_key'], certificate['cache_key'])
            streams[2].packed[:] = streams[1].packed
            with self.assertRaises(Sam2IdentityCollisionError):
                validate_identity_masks(people, streams, {1: 'a'*64, 2: 'c'*64}, cache_path=path)

    def test_cached_failure_remains_actionable(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'identity_guard.json'
            people, streams = inputs(np.ones((4, 10, 10), np.uint8), np.ones((4, 10, 10), np.uint8))
            for _ in range(2):
                with self.assertRaisesRegex(Sam2IdentityCollisionError, 'source frames 0–3'):
                    validate_identity_masks(people, streams, {1: 'a'*64, 2: 'b'*64}, cache_path=path)

    def test_loader_guard_is_mandatory_and_closes_maps_on_error(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            people = []
            masks = np.ones((4, 8, 10), np.uint8)
            for cid in (1, 2):
                np.save(root / f'mask{cid}.npy', np.packbits(masks.reshape(4, -1), axis=1))
                np.savez(root / f'boxes{cid}.npz', frames=np.arange(4), body_boxes=np.tile([0, 0, 10, 8], (4, 1)))
                people.append(dict(canonical_id=cid, object_id=cid, bytetrack_ids=[cid], frame_range=[0, 3],
                                   mask_file=f'mask{cid}.npy', boxes_file=f'boxes{cid}.npz'))
            # No checksum in old manifest: actual bytes still determine cache validity.
            manifest = dict(version=1, run_id='legacy', image_size=[10, 8], num_frames=4, people=people)
            (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            opened = []
            original = np.load
            def capture(*args, **kwargs):
                value = original(*args, **kwargs)
                if kwargs.get('mmap_mode'):
                    opened.append(value)
                return value
            with patch('flowhmr.utils.sam2_person_inputs.np.load', side_effect=capture):
                with self.assertRaises(Sam2IdentityCollisionError):
                    load_person_inputs(root)
            self.assertTrue(all(item._mmap.closed for item in opened))
            # Repair pixels, keeping the manifest unchanged; failure cert must invalidate.
            np.save(root / 'mask2.npy', np.packbits(np.zeros_like(masks).reshape(4, -1), axis=1))
            result, streams, _ = load_person_inputs(root)
            self.assertEqual(len(result.tracks), 2)
            for stream in streams.values():
                stream.close()
            # Put clone back; pass certificate must also invalidate by actual bytes.
            np.save(root / 'mask2.npy', np.packbits(masks.reshape(4, -1), axis=1))
            with self.assertRaises(Sam2IdentityCollisionError):
                load_person_inputs(root)

    def test_loader_failure_after_first_mapping_closes_it(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            np.save(root / 'mask.npy', np.zeros((4, 10), np.uint8))
            np.savez(root / 'boxes.npz', frames=np.arange(4), body_boxes=np.tile([0, 0, 10, 8], (4, 1)))
            person = dict(canonical_id=1, object_id=1, bytetrack_ids=[1], frame_range=[0, 3],
                          mask_file='mask.npy', boxes_file='boxes.npz')
            second = {**person, 'canonical_id': 2, 'object_id': 2, 'mask_file': 'missing.npy'}
            (root / 'manifest.json').write_text(json.dumps(dict(version=1, run_id='test', image_size=[10, 8],
                                                                num_frames=4, people=[person, second])), encoding='utf-8')
            opened = []
            original = np.load
            def capture(*args, **kwargs):
                value = original(*args, **kwargs)
                if kwargs.get('mmap_mode'):
                    opened.append(value)
                return value
            with patch('flowhmr.utils.sam2_person_inputs.np.load', side_effect=capture):
                with self.assertRaises(FileNotFoundError):
                    load_person_inputs(root)
            self.assertTrue(all(item._mmap.closed for item in opened))

    def test_settings_validation(self):
        for kwargs in (dict(min_iou=-1), dict(min_intersection_fraction=2), dict(consecutive_frames=0), dict(consecutive_frames=True)):
            with self.assertRaises(ValueError):
                IdentityGuardSettings(**kwargs)


if __name__ == '__main__':
    unittest.main()
