"""Lossless masks reach the actual batched SAM interface, including hand stride 4."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import cv2
import numpy as np
import torch
from flowhmr.utils.sam2_person_inputs import (PackedMasks, file_sha256, load_person_inputs,
    save_prepared_track, prepared_detection_matches, mask_stream_for_bbox)


class PreparedMasksTest(unittest.TestCase):
    def test_every_fragment_and_empty_mask_survives_packing_and_slicing(self):
        masks = np.zeros((5, 8, 11), np.uint8)
        masks[0, 0, 0] = 1
        masks[1, 7, 10] = 1
        masks[3, :, 4] = 1
        bits = np.packbits(masks.reshape(5, -1), axis=1)
        stream = PackedMasks(bits, 8, 11)
        np.testing.assert_array_equal(stream[:], masks)
        np.testing.assert_array_equal(stream.window(1, 4)[:], masks[1:4])
        self.assertEqual(stream[2].sum(), 0)
        self.assertEqual(stream[0].sum(), 1)

    def test_prepared_identity_and_mask_follow_the_selected_bbox_range(self):
        with TemporaryDirectory() as work:
            root = Path(work)
            video = root / 'clean.mp4'
            video.write_bytes(b'clean video bytes')
            masks = np.zeros((6, 8, 10), np.uint8)
            masks[:, 1:7, 2:8] = 1
            np.save(root / 'masks.npy', np.packbits(masks.reshape(6, -1), axis=1))
            np.savez(root / 'boxes.npz', frames=np.arange(6), body_boxes=np.tile([1, 0, 9, 8], (6, 1)))
            meta = dict(version=1, run_id='run1', video_sha256=file_sha256(video), image_size=[10, 8], num_frames=6,
                        people=[dict(canonical_id=7, object_id=2, bytetrack_ids=[7, 11], frame_range=[0, 5],
                                     mask_file='masks.npy', boxes_file='boxes.npz')])
            (root / 'manifest.json').write_text(json.dumps(meta), encoding='utf-8')
            result, streams, loaded = load_person_inputs(root, video)
            self.assertEqual(len(result.tracks), 1)
            self.assertEqual(result.tracks[0].bytetrack_ids, (7, 11))
            save_prepared_track(root / 'bbox.npz', result, 0, (0, None), loaded)
            self.assertTrue(prepared_detection_matches(root / 'bbox.npz', (0, None), loaded))
            self.assertFalse(prepared_detection_matches(root / 'bbox.npz', (0, None), {**loaded, 'run_id': 'other'}))
            with np.load(root / 'bbox.npz') as data:
                payload = dict(data)
            payload['bbox'] = payload['bbox'][2:5]
            payload['start_end'] = [2, 4]
            np.savez(root / 'bbox.npz', **payload)
            stream, _ = mask_stream_for_bbox(root, root / 'bbox.npz', video)
            np.testing.assert_array_equal(stream[:], masks[2:5])
            video.write_bytes(b'annotated preview')
            with self.assertRaisesRegex(ValueError, 'different clean video'):
                load_person_inputs(root, video)
            stream.close()
            for mapped in streams.values():
                mapped.close()

    def test_real_sam_batch_interface_reads_identical_masks_with_hand_stride_four(self):
        from flowhmr.utils.runtime_sam_features import Sam3DTokenExtractor
        from flowhmr.utils import runtime_video_processing as video
        masks = np.zeros((6, 8, 10), np.uint8)
        masks[0, 1, 1] = 1
        masks[2:, 2:7, 3:8] = 1
        stream = PackedMasks(np.packbits(masks.reshape(6, -1), axis=1), 8, 10)
        observed = []
        class Capture:
            pos = 0
            def isOpened(self): return True
            def get(self, prop): return 6 if prop == cv2.CAP_PROP_FRAME_COUNT else self.pos
            def read(self):
                self.pos += 1
                return self.pos <= 6, np.zeros((8, 10, 3), np.uint8)
            def release(self): pass
        extractor = Sam3DTokenExtractor.__new__(Sam3DTokenExtractor)
        extractor.estimator = SimpleNamespace(device='cpu')
        extractor.frame_batch_size = 2
        extractor.hand_stride, extractor.hand_seed = 4, 123
        extractor._body_tokens, extractor._hand_tokens = [], []
        def extract(images, boxes, cameras, **kwargs):
            observed.append(kwargs['masks'].copy())
            tokens = torch.ones((len(images), 3072))
            indices = kwargs['hand_indices']
            if indices is not None:
                tokens[:, 1024:] = float('nan')
                tokens[indices, 1024:] = 1
            return tokens
        extractor.extract_frames = extract
        with patch.object(video.cv2, 'VideoCapture', return_value=Capture()):
            tokens = extractor.extract_video_tokens('clean.mp4', bbox_xyxy=torch.ones((6, 4)),
                K_all=torch.eye(3).repeat(6, 1, 1), masks=stream, frame_batch_size=2,
                sam_hand_stride=4, use_gpu_preprocess=False)
        np.testing.assert_array_equal(np.concatenate(observed), masks)
        self.assertEqual(tuple(tokens.shape), (6, 3072))
        self.assertTrue(torch.isfinite(tokens).all())


if __name__ == '__main__':
    unittest.main()
