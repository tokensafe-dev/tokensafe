"""
Unit tests for TokenSafe Image Downscaler & Vision Token Optimizer.
"""

import unittest
import os
import shutil
import tempfile
from PIL import Image
from src.image_downscale import ImageDownscaler, estimate_vision_tokens


class TestImageDownscaler(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="tokensafe_img_")

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_vision_token_estimation(self):
        # 512x512 = 1 tile = 85 + 170 = 255 tokens
        self.assertEqual(estimate_vision_tokens(512, 512), 255)
        # 1024x1024 = 4 tiles = 85 + 4*170 = 765 tokens
        self.assertEqual(estimate_vision_tokens(1024, 1024), 765)
        # 0x0
        self.assertEqual(estimate_vision_tokens(0, 0), 0)

    def test_downscale_oversized_image(self):
        # Create a mock 4K screenshot (3840x2160)
        img_path = os.path.join(self.tmp_dir, "screenshot.png")
        img = Image.new("RGB", (3840, 2160), color="white")
        img.save(img_path)

        res = ImageDownscaler.optimize(img_path, max_edge=1024, cache_dir=self.tmp_dir)

        self.assertTrue(res["success"])
        self.assertTrue(res["was_downscaled"])
        self.assertEqual(res["orig_dimensions"], (3840, 2160))
        self.assertEqual(res["new_dimensions"], (1024, 576))
        self.assertGreater(res["saved_tokens"], 1000)
        self.assertTrue(os.path.exists(res["optimized_path"]))

        # Verify original file was not mutated
        with Image.open(img_path) as orig_check:
            self.assertEqual(orig_check.size, (3840, 2160))

    def test_no_downscale_for_small_image(self):
        img_path = os.path.join(self.tmp_dir, "small.png")
        img = Image.new("RGB", (400, 300), color="red")
        img.save(img_path)

        res = ImageDownscaler.optimize(img_path, max_edge=1024, cache_dir=self.tmp_dir)

        self.assertTrue(res["success"])
        self.assertFalse(res["was_downscaled"])
        self.assertEqual(res["saved_tokens"], 0)
        self.assertEqual(res["optimized_path"], img_path)


if __name__ == "__main__":
    unittest.main()
