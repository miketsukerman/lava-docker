#!/usr/bin/env python3

import argparse
import copy
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


def import_bsp_api():
    try:
        from bsp.registry_fetcher import RegistryFetcher, DEFAULT_BRANCH, DEFAULT_REMOTE_URL
        from bsp.utils import get_registry_from_yaml_file
        return RegistryFetcher, DEFAULT_BRANCH, DEFAULT_REMOTE_URL, get_registry_from_yaml_file
    except ImportError:
        try:
            from bsp import RegistryFetcher, DEFAULT_BRANCH, DEFAULT_REMOTE_URL, get_registry_from_yaml_file
            return RegistryFetcher, DEFAULT_BRANCH, DEFAULT_REMOTE_URL, get_registry_from_yaml_file
        except ImportError as exc:
            raise ImportError(
                "bsp-registry-tools is required; install with 'pip install bsp-registry-tools'"
            ) from exc


def get_default_remote_settings() -> tuple:
    fallback_branch = "main"
    fallback_remote_url = "https://github.com/Advantech-EECC/bsp-registry.git"
    try:
        _, default_branch, default_remote_url, _ = import_bsp_api()
        return default_branch, default_remote_url
    except Exception:
        return fallback_branch, fallback_remote_url


@dataclass
class BoardCandidate:
    slug: str
    board_type: str
    vendor: str
    soc_vendor: str


class RegistryClient:
    def __init__(
        self,
        registry_path: Optional[str],
        remote: str,
        branch: str,
        update: bool,
        local_only: bool,
    ):
        self.registry_path = registry_path
        self.remote = remote
        self.branch = branch
        self.update = update
        self.local_only = local_only

    def resolve_registry_path(self) -> Path:
        if self.registry_path:
            path = Path(self.registry_path)
            if not path.is_file():
                raise ValueError(f"Registry file not found: {path}")
            return path

        if self.local_only:
            raise ValueError("--local requires --registry to be set")

        RegistryFetcher, _, _, _ = import_bsp_api()
        fetcher = RegistryFetcher()
        return fetcher.fetch_registry(repo_url=self.remote, branch=self.branch, update=self.update)

    def load_devices(self) -> List[Any]:
        registry_file = self.resolve_registry_path()
        _, _, _, get_registry_from_yaml_file = import_bsp_api()
        model = get_registry_from_yaml_file(registry_file)
        registry = getattr(model, "registry", None)
        if registry is None:
            return []
        devices = getattr(registry, "devices", None)
        if devices is None:
            return []
        return devices


def parse_args() -> argparse.Namespace:
    default_branch, default_remote_url = get_default_remote_settings()
    parser = argparse.ArgumentParser(description="Generate lava-docker boards.yaml from bsp-registry")
    parser.add_argument("--registry", help="Path to local bsp-registry YAML file")
    parser.add_argument("--remote", default=default_remote_url, help="Remote bsp-registry git URL")
    parser.add_argument("--branch", default=default_branch, help="Remote bsp-registry branch")
    parser.add_argument("--no-update", action="store_true", help="Do not update remote cached registry")
    parser.add_argument("--local", action="store_true", help="Do not fetch remote registry; use --registry")

    parser.add_argument("--device", action="append", dest="devices", help="Filter by device slug (repeatable)")
    parser.add_argument("--vendor", action="append", dest="vendors", help="Filter by vendor (repeatable)")
    parser.add_argument("--soc-vendor", action="append", dest="soc_vendors", help="Filter by SoC vendor (repeatable)")
    parser.add_argument("--name-regex", help="Regex filter applied to device slug")

    parser.add_argument("--config", help="Path to overrides/defaults YAML")
    parser.add_argument("--template", help="Optional existing boards YAML to merge with")
    parser.add_argument("-o", "--output", default="boards.yaml", help="Output boards.yaml path")

    parser.add_argument("--master-name", default="master1")
    parser.add_argument("--master-host", default="local")
    parser.add_argument("--master-user", default="admin")
    parser.add_argument("--master-token", default="longrandomtokenadmin")
    parser.add_argument("--master-password", default="admin")

    parser.add_argument("--slave-name", default="lab-slave-0")
    parser.add_argument("--slave-host", default="local")
    parser.add_argument("--slave-remote-master", default=None)
    parser.add_argument("--slave-remote-user", default=None)
    parser.add_argument("--slave-dispatcher-ip", default=None)

    return parser.parse_args()


def load_yaml_file(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML file must contain a mapping: {path}")
    return data


def merge_dict(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = merge_dict(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def normalize_candidates(devices: List[Any], config: Dict[str, Any], args: argparse.Namespace) -> List[BoardCandidate]:
    type_map = config.get("type_map", {})
    if not isinstance(type_map, dict):
        raise ValueError("config.type_map must be a mapping")

    selected_devices = set(args.devices or [])
    selected_vendors = set(args.vendors or [])
    selected_soc_vendors = set(args.soc_vendors or [])
    name_regex = re.compile(args.name_regex) if args.name_regex else None

    candidates: List[BoardCandidate] = []
    for dev in devices:
        slug = getattr(dev, "slug", None)
        if not slug:
            print("WARNING: skip device entry without slug", file=sys.stderr)
            continue
        vendor = getattr(dev, "vendor", "") or ""
        soc_vendor = getattr(dev, "soc_vendor", "") or ""

        if selected_devices and slug not in selected_devices:
            continue
        if selected_vendors and vendor not in selected_vendors:
            continue
        if selected_soc_vendors and soc_vendor not in selected_soc_vendors:
            continue
        if name_regex and not name_regex.search(slug):
            continue

        board_type = str(type_map.get(slug, slug))
        if not board_type:
            print(f"WARNING: skip device '{slug}' with empty mapped type", file=sys.stderr)
            continue
        candidates.append(BoardCandidate(slug=slug, board_type=board_type, vendor=vendor, soc_vendor=soc_vendor))

    candidates.sort(key=lambda x: (x.board_type, x.slug))
    return candidates


def build_boards(candidates: List[BoardCandidate], config: Dict[str, Any]) -> List[Dict[str, Any]]:
    board_defaults = config.get("board_defaults", {})
    board_overrides = config.get("board_overrides", {})
    by_type = board_overrides.get("by_type", {}) if isinstance(board_overrides, dict) else {}
    by_slug = board_overrides.get("by_slug", {}) if isinstance(board_overrides, dict) else {}

    if not isinstance(board_defaults, dict):
        raise ValueError("config.board_defaults must be a mapping")
    if not isinstance(by_type, dict) or not isinstance(by_slug, dict):
        raise ValueError("config.board_overrides.by_type and by_slug must be mappings")

    counters: Dict[str, int] = {}
    boards: List[Dict[str, Any]] = []

    for item in candidates:
        counters[item.board_type] = counters.get(item.board_type, 0) + 1
        sequence = counters[item.board_type]
        board_name = f"{item.board_type}-{sequence:02d}"

        board = copy.deepcopy(board_defaults)
        board = merge_dict(board, by_type.get(item.board_type, {}))
        board = merge_dict(board, by_slug.get(item.slug, {}))

        board["name"] = board_name
        board["type"] = item.board_type
        if "slave" not in board:
            board["slave"] = f"{board_name}-slave"

        boards.append(board)

    if not boards:
        raise ValueError("No boards could be generated from the selected registry entries")

    return boards


def build_default_master_slave(args: argparse.Namespace, boards: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    master_name = args.master_name
    slave_remote_master = args.slave_remote_master or master_name
    slave_remote_user = args.slave_remote_user or args.master_user

    master = {
        "name": master_name,
        "host": args.master_host,
        "users": [
            {
                "name": args.master_user,
                "token": args.master_token,
                "password": args.master_password,
                "superuser": True,
                "staff": True,
            }
        ],
    }

    seen = set()
    slaves = []
    for board in boards:
        slave_name = board.get("slave")
        if not slave_name:
            raise ValueError(f"Board '{board.get('name', '<unknown>')}' has no slave assigned")
        if slave_name in seen:
            raise ValueError(f"Dedicated slave mapping violation: duplicate slave '{slave_name}'")
        seen.add(slave_name)
        slave = {
            "name": slave_name,
            "host": args.slave_host,
            "remote_master": slave_remote_master,
            "remote_user": slave_remote_user,
        }
        if args.slave_dispatcher_ip:
            slave["dispatcher_ip"] = args.slave_dispatcher_ip
        slaves.append(slave)

    if not slaves:
        raise ValueError("No slaves generated; no boards available for dedicated slave assignment")

    return {"masters": [master], "slaves": slaves}


def validate_board_slave_mapping(boards: List[Dict[str, Any]], slaves: List[Dict[str, Any]]) -> None:
    if not boards:
        raise ValueError("boards must be a non-empty list")
    slave_names = set()
    for slave in slaves:
        name = slave.get("name")
        if not name:
            raise ValueError("Each slave entry must have a name")
        slave_names.add(name)

    seen_board_slaves = set()
    for board in boards:
        board_name = board.get("name", "<unknown>")
        board_slave = board.get("slave")
        if not board_slave:
            raise ValueError(f"Board '{board_name}' has no slave assigned")
        if board_slave in seen_board_slaves:
            raise ValueError(
                f"Dedicated slave mapping violation: slave '{board_slave}' is assigned to multiple boards"
            )
        if board_slave not in slave_names:
            raise ValueError(
                f"Board '{board_name}' references missing slave '{board_slave}'. "
                "Provide matching slave entries in config/template or rely on defaults."
            )
        seen_board_slaves.add(board_slave)


def build_output_document(
    template_data: Dict[str, Any],
    config: Dict[str, Any],
    defaults: Dict[str, List[Dict[str, Any]]],
    boards: List[Dict[str, Any]],
) -> Dict[str, Any]:
    data = copy.deepcopy(template_data) if template_data else {}

    masters = config.get("masters")
    slaves = config.get("slaves")

    if masters is None:
        masters = data.get("masters", defaults["masters"])
    if slaves is None:
        slaves = data.get("slaves", defaults["slaves"])

    if not isinstance(masters, list) or not masters:
        raise ValueError("masters must be a non-empty list")
    if not isinstance(slaves, list) or not slaves:
        raise ValueError("slaves must be a non-empty list")

    default_master_name = None
    if defaults.get("masters"):
        default_master_name = defaults["masters"][0].get("name")
    effective_master_name = masters[0].get("name")
    if default_master_name and effective_master_name and default_master_name != effective_master_name:
        normalized_slaves = []
        for slave in slaves:
            updated = copy.deepcopy(slave)
            if updated.get("remote_master") in (None, default_master_name):
                updated["remote_master"] = effective_master_name
            normalized_slaves.append(updated)
        slaves = normalized_slaves

    validate_board_slave_mapping(boards, slaves)
    data["masters"] = masters
    data["slaves"] = slaves
    data["boards"] = boards
    return data


def main() -> int:
    args = parse_args()

    config = load_yaml_file(args.config)
    template_data = load_yaml_file(args.template)

    client = RegistryClient(
        registry_path=args.registry,
        remote=args.remote,
        branch=args.branch,
        update=not args.no_update,
        local_only=args.local,
    )

    devices = client.load_devices()
    candidates = normalize_candidates(devices, config, args)

    boards = build_boards(candidates, config)
    defaults = build_default_master_slave(args, boards)

    output_data = build_output_document(template_data, config, defaults, boards)

    output_path = Path(args.output)
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(output_data, f, sort_keys=False)

    print(f"Generated {len(boards)} board entries in {output_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
