"""Optional SAM 2.1 runtime component: verified install, extraction scope, gating."""
import hashlib
import io
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from flowhmr import model_downloads, model_files as files, model_sources as sources, sam2_setup  # noqa: E402


def build_archive(members):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class Sam2SetupTest(unittest.TestCase):
    """The installer keeps only the inference package and verifies the archive."""

    def setUp(self):
        self.revision = sources.SAM2_SOURCE_REVISION
        self.prefix = f"sam2-{self.revision}/"
        members = {
            self.prefix + "README.md": "not needed",
            self.prefix + "demo/demo.py": "demo code is not shipped",
            self.prefix + "sam2/__init__.py": "",
            self.prefix + "sam2/sam2_video_predictor.py": "predictor",
            self.prefix + "sam2/modeling/__init__.py": "",
            self.prefix + "sam2/modeling/sam2_base.py": "base",
            self.prefix + "sam2/utils/__init__.py": "",
            self.prefix + "sam2/utils/misc.py": "misc",
            self.prefix + "sam2/configs/sam2.1/sam2.1_hiera_s.yaml": "model: {}",
            self.prefix + "sam2/notebooks/demo.ipynb": "{}",
        }
        self.data = build_archive(members)
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.temp = TemporaryDirectory()
        self.runtime = Path(self.temp.name) / "sam2_runtime"
        self.patchers = [patch.object(files, "SAM2_RUNTIME", self.runtime),
                         patch.object(sources, "SAM2_SOURCE_SHA256", self.digest)]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def archive(self):
        path = Path(self.temp.name) / "sam2-source.zip"
        path.write_bytes(self.data)
        return path

    def test_install_extracts_only_the_package(self):
        self.assertFalse(sam2_setup.runtime_installed())
        sam2_setup.install_code(self.archive())
        self.assertTrue(sam2_setup.runtime_installed())
        self.assertTrue((self.runtime / "sam2/sam2_video_predictor.py").is_file())
        self.assertTrue((self.runtime / "sam2/configs/sam2.1/sam2.1_hiera_s.yaml").is_file())
        self.assertFalse((self.runtime / "sam2/notebooks/demo.ipynb").exists())
        self.assertFalse((self.runtime / "demo").exists())
        self.assertEqual((self.runtime / "revision.txt").read_text(encoding="ascii").strip(), self.revision)
        # The official Hiera backbone imports iopath at module import time.
        self.assertTrue((self.runtime / "iopath/common/file_io.py").is_file())

    def test_bad_archive_is_rejected(self):
        with patch.object(sources, "SAM2_SOURCE_SHA256", "0" * 64):
            with self.assertRaises(sam2_setup.BadArchive):
                sam2_setup.install_code(self.archive())
        self.assertFalse(sam2_setup.runtime_installed())
        self.assertFalse((self.runtime / "revision.txt").exists())

    def test_crossing_issue_reports_the_missing_piece(self):
        with patch.object(files, "crossing_optimization_available", return_value=False):
            self.assertEqual(sam2_setup.crossing_issue()[0], "crossing_model_missing")
        with patch.object(files, "crossing_optimization_available", return_value=True), \
                patch.object(sam2_setup, "code_available", return_value=False):
            self.assertEqual(sam2_setup.crossing_issue()[0], "crossing_code_missing")
        with patch.object(files, "crossing_optimization_available", return_value=True), \
                patch.object(sam2_setup, "code_available", return_value=True):
            self.assertIsNone(sam2_setup.crossing_issue())


class OptionalDownloadPlanTest(unittest.TestCase):
    """The torso base and SAM 2 are opt-in, like HaMeR; "download all" skips them."""

    def setUp(self):
        self.base_asset = sources.Asset(files.FLOWHMR_BASE_CKPT, 10, "0" * 64)
        self.sam2_asset = sources.Asset(files.SAM2_CROSSING, 10, "0" * 64)
        self.patchers = [
            patch.object(sources, "PUBLIC_ASSETS",
                         {"flowhmr_base": (self.base_asset,), "sam2": (self.sam2_asset,)}),
            patch.object(files, "file_present", return_value=False),
            patch.object(files, "resolve_model", side_effect=lambda relative: Path(relative)),
            patch.object(sam2_setup, "code_available", return_value=False),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()

    def test_default_plan_has_no_optional_components(self):
        self.assertEqual(model_downloads.missing_downloads("cpu"), {})

    def test_explicit_groups_include_their_component(self):
        base = model_downloads.missing_downloads("cpu", include_base=True)
        self.assertEqual(base, {"flowhmr_base": (self.base_asset,)})
        crossing = model_downloads.missing_downloads("cpu", include_crossing=True)
        self.assertEqual(crossing, {"sam2": (sources.SAM2_CODE, self.sam2_asset)})

    def test_installed_component_is_not_downloaded_again(self):
        with patch.object(sam2_setup, "code_available", return_value=True):
            crossing = model_downloads.missing_downloads("cpu", include_crossing=True)
        self.assertEqual(crossing, {"sam2": (self.sam2_asset,)})


if __name__ == "__main__":
    unittest.main()
