#!/usr/bin/env python3

import argparse
import io
import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from contextlib import redirect_stderr

import yaml

REPO = Path(__file__).resolve().parents[1]
FIX = REPO / "tests" / "bsp_boards_gen"
SPEC = importlib.util.spec_from_file_location("bsp_boards_gen_script", REPO / "bsp_boards_gen.py")
bsp_boards_gen = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bsp_boards_gen)


def load_yaml(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def fixture_devices():
    data = load_yaml(FIX / "registry-fixture.yml")
    result = []
    for entry in data["registry"]["devices"]:
        result.append(SimpleNamespace(**entry))
    return result


def default_args(**kwargs):
    values = {
        "devices": None,
        "vendors": None,
        "soc_vendors": None,
        "name_regex": None,
        "master_name": "master1",
        "master_host": "local",
        "master_user": "admin",
        "master_token": "longrandomtokenadmin",
        "master_password": "admin",
        "slave_name": "lab-slave-0",
        "slave_host": "local",
        "slave_remote_master": None,
        "slave_remote_user": None,
        "slave_dispatcher_ip": None,
    }
    values.update(kwargs)
    return argparse.Namespace(**values)


class TestBspBoardsGen(unittest.TestCase):
    def test_minimal_generation(self):
        args = default_args()
        config = {}
        candidates = bsp_boards_gen.normalize_candidates(fixture_devices(), config, args)
        boards = bsp_boards_gen.build_boards(candidates, config)
        defaults = bsp_boards_gen.build_default_master_slave(args, boards)
        output = bsp_boards_gen.build_output_document({}, config, defaults, boards)

        self.assertEqual(output, load_yaml(FIX / "expected-minimal.yml"))

    def test_overrides_and_same_type_numbering(self):
        args = default_args()
        config = load_yaml(FIX / "config-overrides.yml")
        candidates = bsp_boards_gen.normalize_candidates(fixture_devices(), config, args)
        boards = bsp_boards_gen.build_boards(candidates, config)
        defaults = bsp_boards_gen.build_default_master_slave(args, boards)
        output = bsp_boards_gen.build_output_document({}, config, defaults, boards)

        self.assertEqual(output, load_yaml(FIX / "expected-overrides.yml"))

    def test_missing_required_data_fails(self):
        args = default_args(devices=["does-not-exist"])
        candidates = bsp_boards_gen.normalize_candidates(fixture_devices(), {}, args)
        self.assertEqual(candidates, [])
        with self.assertRaises(ValueError):
            bsp_boards_gen.build_boards(candidates, {})

    def test_dedicated_slave_validation_rejects_duplicate_slave(self):
        boards = [
            {"name": "qemu-01", "type": "qemu", "slave": "lab-slave-shared"},
            {"name": "qemu-02", "type": "qemu", "slave": "lab-slave-shared"},
        ]
        slaves = [{"name": "lab-slave-shared"}]
        with self.assertRaises(ValueError):
            bsp_boards_gen.validate_board_slave_mapping(boards, slaves)

    def test_invalid_slug_warning_emitted(self):
        args = default_args()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            bsp_boards_gen.normalize_candidates(fixture_devices(), {}, args)
        self.assertIn("skip device entry without slug", stderr.getvalue())

    def test_registry_client_layer_with_mocked_api(self):
        calls = {"fetched": False}

        class FakeFetcher:
            def fetch_registry(self, repo_url, branch, update):
                calls["fetched"] = True
                return FIX / "registry-fixture.yml"

        def fake_get_registry(path):
            data = load_yaml(path)
            devices = [SimpleNamespace(**entry) for entry in data["registry"]["devices"]]
            return SimpleNamespace(registry=SimpleNamespace(devices=devices))

        with patch.object(
            bsp_boards_gen,
            "import_bsp_api",
            return_value=(FakeFetcher, "main", "url", fake_get_registry),
        ):
            client = bsp_boards_gen.RegistryClient(
                registry_path=None,
                remote="https://example.invalid/repo.git",
                branch="main",
                update=False,
                local_only=False,
            )
            devices = client.load_devices()

        self.assertTrue(calls["fetched"])
        self.assertEqual(len(devices), 4)

    def test_integration_with_lavalab_gen(self):
        args = default_args()
        config = {}
        candidates = bsp_boards_gen.normalize_candidates(fixture_devices(), config, args)
        boards = bsp_boards_gen.build_boards(candidates, config)
        defaults = bsp_boards_gen.build_default_master_slave(args, boards)
        output = bsp_boards_gen.build_output_document({}, config, defaults, boards)

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            boards_path = tmp_path / "boards.yaml"
            out_dir = tmp_path / "out"
            with open(boards_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(output, f, sort_keys=False)

            ret = subprocess.run(
                ["python3", str(REPO / "lavalab-gen.py"), "-o", str(out_dir), str(boards_path)],
                cwd=str(REPO),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(ret.returncode, 0, msg=ret.stdout + "\n" + ret.stderr)
            self.assertTrue((out_dir / "local" / "docker-compose.yml").is_file())
            self.assertTrue((out_dir / "local" / "imx93rom2820a1-01-slave" / "devices").is_dir())


if __name__ == "__main__":
    unittest.main()
