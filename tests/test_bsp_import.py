#!/usr/bin/env python3
"""
Unit tests for bsp_import.py.

Covers:
  - parse_bsp_registry_file()  (v2.0 schema)
  - _parse_bsp_list_output()
  - load_overlay()
  - merge_boards()
  - import_boards_from_registry()  (integration, no network)
  - run_import_mode() CLI (integration, no network, no bsp tool)
  - Error / warning paths
"""

import os
import sys
import tempfile
import unittest
import yaml

# Resolve project root so we can import bsp_import regardless of cwd
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TESTS_DIR)
BSP_IMPORT_FIXTURES = os.path.join(TESTS_DIR, "bsp_import")
sys.path.insert(0, REPO_ROOT)

import bsp_import  # noqa: E402 – must come after sys.path tweak


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fixture(name):
    return os.path.join(BSP_IMPORT_FIXTURES, name)


# ---------------------------------------------------------------------------
# parse_bsp_registry_file
# ---------------------------------------------------------------------------

class TestParseBspRegistryFile(unittest.TestCase):

    def test_v2_schema_parses_correctly(self):
        devices = bsp_import.parse_bsp_registry_file(_fixture("bsp-registry.yaml"))
        self.assertEqual(len(devices), 3)
        slugs = {d["slug"] for d in devices}
        self.assertIn("imx8mp-lpddr4-evk", slugs)
        self.assertIn("rsb3720", slugs)
        self.assertIn("qemuarm64", slugs)

    def test_device_metadata_populated(self):
        devices = bsp_import.parse_bsp_registry_file(_fixture("bsp-registry.yaml"))
        imx = next(d for d in devices if d["slug"] == "imx8mp-lpddr4-evk")
        self.assertEqual(imx["vendor"], "advantech")
        self.assertEqual(imx["soc_vendor"], "nxp")
        self.assertEqual(imx["description"], "NXP i.MX8MP LPDDR4 EVK")

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            bsp_import.parse_bsp_registry_file("/nonexistent/path/bsp-registry.yaml")

    def test_empty_file_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            tmp = f.name
        try:
            with self.assertRaises(ValueError):
                bsp_import.parse_bsp_registry_file(tmp)
        finally:
            os.unlink(tmp)

    def test_no_devices_raises(self):
        data = {"specification": {"version": "2.0"}, "registry": {}}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(data, f)
            tmp = f.name
        try:
            with self.assertRaises(ValueError):
                bsp_import.parse_bsp_registry_file(tmp)
        finally:
            os.unlink(tmp)

    def test_legacy_bsp_list_format(self):
        data = {
            "bsp": [
                {"name": "my-board", "description": "My Test Board"},
                {"name": "other-board"},
            ]
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(data, f)
            tmp = f.name
        try:
            devices = bsp_import.parse_bsp_registry_file(tmp)
            self.assertEqual(len(devices), 2)
            self.assertEqual(devices[0]["slug"], "my-board")
            self.assertEqual(devices[0]["description"], "My Test Board")
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# _parse_bsp_list_output
# ---------------------------------------------------------------------------

class TestParseBspListOutput(unittest.TestCase):

    def test_standard_output(self):
        output = (
            "- qemuarm64: QEMU ARM64 (vendor: qemu)\n"
            "- imx8mp-lpddr4-evk: NXP i.MX8MP LPDDR4 EVK (vendor: advantech, soc_vendor: nxp)\n"
        )
        devices = bsp_import._parse_bsp_list_output(output)
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]["slug"], "qemuarm64")
        self.assertEqual(devices[0]["description"], "QEMU ARM64")
        self.assertEqual(devices[1]["slug"], "imx8mp-lpddr4-evk")

    def test_output_without_bullet(self):
        output = "qemuarm64: QEMU ARM64\nimx8mp: NXP board\n"
        devices = bsp_import._parse_bsp_list_output(output)
        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]["slug"], "qemuarm64")

    def test_empty_output_returns_empty_list(self):
        devices = bsp_import._parse_bsp_list_output("")
        self.assertEqual(devices, [])

    def test_comment_lines_ignored(self):
        output = "# comment\n- qemuarm64: QEMU ARM64\n"
        devices = bsp_import._parse_bsp_list_output(output)
        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0]["slug"], "qemuarm64")


# ---------------------------------------------------------------------------
# load_overlay
# ---------------------------------------------------------------------------

class TestLoadOverlay(unittest.TestCase):

    def test_loads_valid_overlay(self):
        overlay = bsp_import.load_overlay(_fixture("overlay.yaml"))
        self.assertIn("boards", overlay)
        self.assertEqual(len(overlay["boards"]), 2)
        self.assertEqual(overlay["slave"], "lab-slave-0")

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            bsp_import.load_overlay("/nonexistent/overlay.yaml")

    def test_overlay_without_boards_raises(self):
        data = {"slave": "lab-slave-0"}
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            yaml.dump(data, f)
            tmp = f.name
        try:
            with self.assertRaises(ValueError):
                bsp_import.load_overlay(tmp)
        finally:
            os.unlink(tmp)

    def test_empty_overlay_raises(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write("")
            tmp = f.name
        try:
            with self.assertRaises(ValueError):
                bsp_import.load_overlay(tmp)
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# merge_boards
# ---------------------------------------------------------------------------

class TestMergeBoards(unittest.TestCase):

    def _devices(self):
        return [
            {"slug": "imx8mp-lpddr4-evk", "description": "NXP i.MX8MP"},
            {"slug": "qemuarm64", "description": "QEMU ARM64"},
        ]

    def test_basic_merge(self):
        overlay = {
            "slave": "lab-slave-0",
            "boards": [
                {"type": "imx8mp-lpddr4-evk", "name": "imx8mp-01"},
                {"type": "qemuarm64", "kvm": True},
            ],
        }
        boards, warnings = bsp_import.merge_boards(self._devices(), overlay)
        self.assertEqual(len(boards), 2)
        self.assertEqual(boards[0]["name"], "imx8mp-01")
        self.assertEqual(boards[0]["slave"], "lab-slave-0")
        self.assertEqual(boards[1]["slave"], "lab-slave-0")
        self.assertEqual(warnings, [])

    def test_auto_name_generation(self):
        overlay = {
            "slave": "lab-slave-0",
            "boards": [
                {"type": "qemuarm64"},
                {"type": "qemuarm64"},
            ],
        }
        boards, warnings = bsp_import.merge_boards(self._devices(), overlay)
        self.assertEqual(boards[0]["name"], "qemuarm64-01")
        self.assertEqual(boards[1]["name"], "qemuarm64-02")
        self.assertEqual(warnings, [])

    def test_per_board_slave_overrides_default(self):
        overlay = {
            "slave": "lab-slave-0",
            "boards": [{"type": "qemuarm64", "slave": "lab-slave-1"}],
        }
        boards, _ = bsp_import.merge_boards(self._devices(), overlay)
        self.assertEqual(boards[0]["slave"], "lab-slave-1")

    def test_unknown_type_generates_warning(self):
        overlay = {
            "slave": "lab-slave-0",
            "boards": [{"type": "does-not-exist", "name": "x-01"}],
        }
        boards, warnings = bsp_import.merge_boards(self._devices(), overlay, warn_unknown=True)
        self.assertEqual(len(boards), 1)
        self.assertTrue(any("does-not-exist" in w for w in warnings))

    def test_board_missing_type_skipped_with_warning(self):
        overlay = {
            "slave": "lab-slave-0",
            "boards": [{"name": "no-type-board"}],
        }
        boards, warnings = bsp_import.merge_boards(self._devices(), overlay)
        self.assertEqual(len(boards), 0)
        self.assertTrue(any("no 'type'" in w for w in warnings))

    def test_default_slave_applied_when_absent(self):
        overlay = {
            "boards": [{"type": "qemuarm64"}],
        }
        boards, _ = bsp_import.merge_boards(self._devices(), overlay)
        self.assertEqual(boards[0]["slave"], "lab-slave-0")


# ---------------------------------------------------------------------------
# import_boards_from_registry  (integration, file-only, no bsp tool)
# ---------------------------------------------------------------------------

class TestImportBoardsFromRegistry(unittest.TestCase):

    def test_success_with_registry_and_overlay(self):
        boards, warnings, devices = bsp_import.import_boards_from_registry(
            registry_path=_fixture("bsp-registry.yaml"),
            overlay_path=_fixture("overlay.yaml"),
        )
        self.assertEqual(len(boards), 2)
        self.assertEqual(warnings, [])
        slugs = {d["slug"] for d in devices}
        self.assertIn("imx8mp-lpddr4-evk", slugs)

    def test_output_matches_expected(self):
        boards, _, _ = bsp_import.import_boards_from_registry(
            registry_path=_fixture("bsp-registry.yaml"),
            overlay_path=_fixture("overlay.yaml"),
        )
        with open(_fixture("expected-boards.yaml")) as f:
            expected = yaml.safe_load(f)
        self.assertEqual(boards, expected["boards"])

    def test_autoname_generates_sequential_names(self):
        boards, warnings, _ = bsp_import.import_boards_from_registry(
            registry_path=_fixture("bsp-registry.yaml"),
            overlay_path=_fixture("overlay-autoname.yaml"),
        )
        self.assertEqual(len(boards), 2)
        names = [b["name"] for b in boards]
        self.assertEqual(names, ["qemuarm64-01", "qemuarm64-02"])
        self.assertEqual(warnings, [])

    def test_unknown_type_produces_warning_not_error(self):
        boards, warnings, _ = bsp_import.import_boards_from_registry(
            registry_path=_fixture("bsp-registry.yaml"),
            overlay_path=_fixture("overlay-unknown-type.yaml"),
        )
        self.assertEqual(len(boards), 1)
        self.assertTrue(len(warnings) > 0)

    def test_registry_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            bsp_import.import_boards_from_registry(
                registry_path="/nonexistent/bsp-registry.yaml",
                overlay_path=_fixture("overlay.yaml"),
            )

    def test_overlay_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            bsp_import.import_boards_from_registry(
                registry_path=_fixture("bsp-registry.yaml"),
                overlay_path="/nonexistent/overlay.yaml",
            )

    def test_no_overlay_generates_one_board_per_device(self):
        boards, _, devices = bsp_import.import_boards_from_registry(
            registry_path=_fixture("bsp-registry.yaml"),
        )
        self.assertEqual(len(boards), len(devices))


# ---------------------------------------------------------------------------
# write_boards_yaml
# ---------------------------------------------------------------------------

class TestWriteBoardsYaml(unittest.TestCase):

    def test_writes_valid_yaml(self):
        boards = [
            {"name": "qemuarm64-01", "type": "qemuarm64", "slave": "lab-slave-0"},
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            tmp = f.name
        try:
            bsp_import.write_boards_yaml(boards, tmp)
            with open(tmp) as f:
                result = yaml.safe_load(f)
            self.assertIn("boards", result)
            self.assertEqual(result["boards"][0]["name"], "qemuarm64-01")
        finally:
            os.unlink(tmp)


# ---------------------------------------------------------------------------
# run_import_mode CLI (integration)
# ---------------------------------------------------------------------------

class TestRunImportMode(unittest.TestCase):

    def test_cli_creates_output_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            output_path = f.name
        os.unlink(output_path)  # run_import_mode should create it
        try:
            argv = [
                "lavalab-gen.py",
                "--import-bsp",
                "--bsp-registry", _fixture("bsp-registry.yaml"),
                "--overlay", _fixture("overlay.yaml"),
                "--output-boards", output_path,
            ]
            bsp_import.run_import_mode(argv)
            self.assertTrue(os.path.isfile(output_path))
            with open(output_path) as f:
                result = yaml.safe_load(f)
            self.assertIn("boards", result)
            self.assertEqual(len(result["boards"]), 2)
        finally:
            if os.path.exists(output_path):
                os.unlink(output_path)

    def test_cli_missing_overlay_file_exits_nonzero(self):
        argv = [
            "lavalab-gen.py",
            "--import-bsp",
            "--bsp-registry", _fixture("bsp-registry.yaml"),
            "--overlay", "/nonexistent/overlay.yaml",
            "--output-boards", "/tmp/out.yaml",
        ]
        with self.assertRaises(SystemExit) as ctx:
            bsp_import.run_import_mode(argv)
        self.assertNotEqual(ctx.exception.code, 0)

    def test_cli_missing_registry_file_exits_nonzero(self):
        argv = [
            "lavalab-gen.py",
            "--import-bsp",
            "--bsp-registry", "/nonexistent/bsp-registry.yaml",
            "--overlay", _fixture("overlay.yaml"),
            "--output-boards", "/tmp/out.yaml",
        ]
        with self.assertRaises(SystemExit) as ctx:
            bsp_import.run_import_mode(argv)
        self.assertNotEqual(ctx.exception.code, 0)

    def test_cli_missing_bsp_tool_and_no_registry_exits_nonzero(self):
        # When neither --bsp-registry nor a reachable bsp tool exists the
        # import mode must exit with a non-zero code and a useful error.
        import unittest.mock as mock
        argv = [
            "lavalab-gen.py",
            "--import-bsp",
            "--overlay", _fixture("overlay.yaml"),
            "--output-boards", "/tmp/out.yaml",
        ]
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(SystemExit) as ctx:
                bsp_import.run_import_mode(argv)
        self.assertNotEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main()
