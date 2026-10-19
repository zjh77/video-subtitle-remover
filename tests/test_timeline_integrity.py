import importlib.util
import shutil
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import cv2
import numpy as np


HAS_AV = importlib.util.find_spec("av") is not None


class PictureComparisonTests(unittest.TestCase):
    def test_flat_frame_tolerance_and_outlier_limit(self):
        from vsr_api.app.timeline_integrity import _same_picture

        source = np.full(1000, 246.0)
        for shift in (2.012, 4, 5):
            self.assertTrue(_same_picture(source, source - shift))
        self.assertFalse(_same_picture(source, source - 5.1))
        # A small average must not hide a larger altered region.
        altered = source.copy()
        altered[:20] -= 10
        self.assertFalse(_same_picture(source, altered))

    def test_excluded_area_matches_expanded_inclusive_rectangle(self):
        from vsr_api.app.timeline_integrity import _outside_subtitles

        frame = np.zeros((96, 96, 3), dtype=np.uint8)
        # A non-default padding also checks coordinates clipped at the edge.
        area = dict(xmin=8, xmax=23, ymin=8, ymax=23)
        cv2.rectangle(frame, (0, 0), (48, 48), (255, 255, 255), -1)
        samples = _outside_subtitles(frame, [area], mask_padding=25)
        self.assertTrue(np.all(samples == 0))
        self.assertEqual(samples.size, 36 - 16)
        frame[64, 64] = 255
        self.assertIn(255, _outside_subtitles(frame, [area], mask_padding=25))

    def test_worker_success_transmits_actual_mask_padding(self):
        import queue
        from vsr_api.app.job_queue import JobQueue

        events = queue.Queue()
        events.put(("success", 25))
        validation = {}
        self.assertTrue(JobQueue()._drain("test", events, validation))
        self.assertEqual(validation, {"mask_padding": 25})
        self.assertFalse(JobQueue()._drain("test", events, validation))
        self.assertEqual(validation["mask_padding"], 25)

    def test_near_white_and_dark_frames_tolerate_small_encoding_noise(self):
        from vsr_api.app.timeline_integrity import _same_picture

        rng = np.random.default_rng(1300)
        for brightness in (8, 247):
            source = np.clip(brightness + rng.normal(0, 2.5, 8192), 0, 255)
            encoded = np.clip(source - 1 + rng.normal(0, 0.8, source.size), 0, 255)
            # Reproduce the old rejection on a low-contrast flash frame.
            self.assertLess(np.corrcoef(source, encoded)[0, 1], 0.96)
            self.assertTrue(_same_picture(source, encoded))

    def test_flat_frame_replacement_and_brightness_change_are_rejected(self):
        from vsr_api.app.timeline_integrity import _same_picture

        white = np.full(8192, 247.0)
        self.assertFalse(_same_picture(white, np.zeros(8192)))
        self.assertFalse(_same_picture(white, white - 10))
        textured = np.tile([225.0, 255.0], 4096)
        self.assertFalse(_same_picture(white, textured))
        self.assertFalse(_same_picture(textured, white))


@unittest.skipUnless(HAS_AV, "PyAV is installed with the VSR runtime dependencies")
class TimelineIntegrityTests(unittest.TestCase):
    def setUp(self):
        import av
        self.av = av
        self.root = Path(tempfile.mkdtemp(prefix="vsr-timeline-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_vfr_source(self, path):
        output = self.av.open(str(path), "w")
        stream = output.add_stream("libx264")
        stream.width, stream.height, stream.pix_fmt = 64, 64, "yuv420p"
        stream.time_base = Fraction(1, 1000)
        stream.codec_context.time_base = Fraction(1, 1000)
        # Deliberately include a hard cut and a variable frame duration.
        for pts, color in [(0, 20), (33, 20), (66, 220), (133, 220), (166, 20)]:
            pixels = np.full((64, 64, 3), color, dtype=np.uint8)
            frame = self.av.VideoFrame.from_ndarray(pixels, format="bgr24")
            frame.pts, frame.time_base = pts, Fraction(1, 1000)
            for packet in stream.encode(frame): output.mux(packet)
        for packet in stream.encode(): output.mux(packet)
        output.close()

    def test_writer_preserves_vfr_pts_and_hard_cut_order(self):
        from backend.tools.video_io import FFmpegVideoWriter
        from vsr_api.app.timeline_integrity import validate_timeline

        source, result = self.root / "source.mp4", self.root / "result.mp4"
        self._write_vfr_source(source)
        capture = cv2.VideoCapture(str(source))
        writer = FFmpegVideoWriter(str(result), 30, (64, 64), str(source))
        while True:
            ok, frame = capture.read()
            if not ok: break
            writer.write(frame)
        capture.release(); writer.release()

        validate_timeline(source, result, [{"xmin": 0, "xmax": 64, "ymin": 56, "ymax": 64}])

    def test_picture_fingerprint_allows_compression_noise_but_not_a_scene_swap(self):
        from vsr_api.app.timeline_integrity import _same_picture

        rng = np.random.default_rng(42)
        source = rng.integers(0, 256, size=1024).astype(np.float32)
        reencoded = np.clip(source + rng.normal(0, 7, size=source.shape), 0, 255)
        different_scene = rng.integers(0, 256, size=1024).astype(np.float32)

        self.assertTrue(_same_picture(source, reencoded))
        self.assertFalse(_same_picture(source, different_scene))


if __name__ == "__main__":
    unittest.main()
