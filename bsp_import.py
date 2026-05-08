#!/usr/bin/env python3
"""
BSP Registry import adapter for lava-docker.

Reads a bsp-registry.yaml (Advantech BSP Registry format v2.0) and maps
device entries to lava-docker board definitions, optionally merged with a
local overlay file containing lab-specific hardware wiring.

Standalone usage (called from lavalab-gen.py --import-bsp):
    python3 bsp_import.py --overlay boards-overlay.yaml \\
        [--bsp-registry bsp-registry.yaml] \\
        [--bsp-remote https://github.com/Advantech-EECC/bsp-registry.git] \\
        [--bsp-branch main] \\
        [--bsp-no-update] \\
        [--output-boards boards-imported.yaml]
"""

from __future__ import print_function
import argparse
import os
import re
import shutil
import subprocess
import sys
import yaml

BSP_TOOL = "bsp"
DEFAULT_OUTPUT = "boards-imported.yaml"
DEFAULT_REMOTE = "https://github.com/Advantech-EECC/bsp-registry.git"
DEFAULT_BRANCH = "main"


# ---------------------------------------------------------------------------
# Registry resolution helpers
# ---------------------------------------------------------------------------

def _find_bsp_tool():
    """Return path to the bsp CLI tool, or None if not found."""
    return shutil.which(BSP_TOOL)


def _run_bsp_list_devices(remote=None, branch=None, no_update=False, registry_path=None):
    """
    Run 'bsp list devices' and return its stdout as a string.
    Raises RuntimeError with an actionable message on failure.
    """
    tool = _find_bsp_tool()
    if not tool:
        raise RuntimeError(
            "The 'bsp' CLI tool was not found on PATH.\n"
            "Install bsp-registry-tools and try again:\n"
            "  pip install bsp-registry-tools\n"
            "Or provide a local registry file via --bsp-registry."
        )

    cmd = [tool]
    if registry_path:
        cmd += ["--registry", registry_path]
    if remote:
        cmd += ["--remote", remote]
    if branch:
        cmd += ["--branch", branch]
    if no_update:
        cmd += ["--no-update"]
    cmd += ["--no-color", "list", "devices"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "The 'bsp list devices' command timed out after 120 s.\n"
            "Check network connectivity, or use --bsp-no-update / --bsp-registry."
        )
    except FileNotFoundError:
        raise RuntimeError(
            f"Could not execute '{cmd[0]}'. Make sure bsp-registry-tools is installed."
        )

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RuntimeError(
            f"'bsp list devices' failed (exit {result.returncode}):\n{stderr}"
        )

    return result.stdout


def _parse_bsp_list_output(output):
    """
    Parse plain-text output of 'bsp list devices'.

    Expected format (one device per line, leading bullet optional):
        - slug: Description (vendor: foo, soc_vendor: bar)
        qemuarm64: QEMU ARM64 (vendor: qemu)

    Returns a list of dicts with at minimum 'slug' and 'description'.
    """
    devices = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- "):
            line = line[2:].strip()
        if ":" not in line:
            continue
        slug, rest = line.split(":", 1)
        slug = slug.strip()
        description = rest.strip()
        # Strip trailing parenthetical metadata: "(vendor: ..., soc_vendor: ...)"
        description = re.sub(r"\s*\(.*\)\s*$", "", description).strip()
        if slug:
            devices.append({"slug": slug, "description": description})
    return devices


# ---------------------------------------------------------------------------
# Direct registry YAML parsing
# ---------------------------------------------------------------------------

def parse_bsp_registry_file(registry_path):
    """
    Parse a local bsp-registry.yaml / bsp-registry.yml file (v2.0 schema).

    Returns a list of device dicts with at minimum 'slug' and optional
    'description', 'vendor', 'soc_vendor', 'soc_family'.

    Raises FileNotFoundError or ValueError on problems.
    """
    if not os.path.isfile(registry_path):
        raise FileNotFoundError(f"BSP registry file not found: {registry_path}")

    with open(registry_path, "r") as fh:
        registry = yaml.safe_load(fh)

    if not registry:
        raise ValueError(f"Empty or unparseable BSP registry file: {registry_path}")

    # -- v2.0 schema: specification.version "2.0", registry.devices list --
    registry_section = registry.get("registry", {})
    device_list = registry_section.get("devices", [])

    if device_list:
        return [
            {
                "slug": dev["slug"],
                "description": dev.get("description", ""),
                "vendor": dev.get("vendor", ""),
                "soc_vendor": dev.get("soc_vendor", ""),
                "soc_family": dev.get("soc_family", ""),
            }
            for dev in device_list
            if isinstance(dev, dict) and "slug" in dev
        ]

    # -- Legacy / older schema: top-level 'bsp' list with 'name' fields --
    bsp_list = registry.get("bsp", [])
    devices = []
    for item in bsp_list:
        if isinstance(item, dict) and "name" in item:
            devices.append({
                "slug": item["name"],
                "description": item.get("description", ""),
                "vendor": item.get("vendor", ""),
                "soc_vendor": "",
                "soc_family": "",
            })
    if devices:
        return devices

    raise ValueError(
        f"Could not find a 'registry.devices' or 'bsp' list in {registry_path}.\n"
        "Ensure the file follows the bsp-registry-tools YAML schema."
    )


# ---------------------------------------------------------------------------
# Overlay loading and board merging
# ---------------------------------------------------------------------------

def load_overlay(overlay_path):
    """
    Load a local overlay YAML file containing lab-specific board wiring.

    Overlay format::

        # Optional default slave applied to all boards
        slave: lab-slave-0

        boards:
          - type: <device-slug>      # required
            name: <lava-device-name> # optional; auto-generated as <type>-01 if absent
            # All other lava-docker board fields are allowed (uart, pdu_generic, …)
            uart:
              idvendor: 0x0403
              idproduct: 0x6001
              serial: ABC123
            pdu_generic:
              hard_reset_command: /usr/bin/pdu reset 1
              power_off_command: /usr/bin/pdu off 1
              power_on_command: /usr/bin/pdu on 1

    Returns the parsed dict.
    Raises FileNotFoundError or ValueError on problems.
    """
    if not os.path.isfile(overlay_path):
        raise FileNotFoundError(f"Overlay file not found: {overlay_path}")

    with open(overlay_path, "r") as fh:
        overlay = yaml.safe_load(fh)

    if not overlay:
        raise ValueError(f"Empty or unparseable overlay file: {overlay_path}")

    if "boards" not in overlay or not isinstance(overlay["boards"], list):
        raise ValueError(
            f"Overlay file '{overlay_path}' must contain a 'boards' list.\n"
            "See boards-overlay.yaml.example for the expected format."
        )

    return overlay


def merge_boards(registry_devices, overlay, warn_unknown=True):
    """
    Merge BSP registry device metadata with overlay lab-specific wiring.

    Args:
        registry_devices: list of device dicts from parse_bsp_registry_file()
                          or _parse_bsp_list_output()
        overlay:          dict loaded by load_overlay()
        warn_unknown:     if True, accumulate warnings for boards whose type
                          is not found in the registry device list

    Returns:
        (boards, warnings) where boards is a list of lava-docker board dicts
        and warnings is a list of human-readable warning strings.
    """
    known_slugs = {dev["slug"] for dev in registry_devices}
    default_slave = overlay.get("slave", "lab-slave-0")

    type_counters = {}
    result_boards = []
    warnings = []

    for raw_board in overlay.get("boards", []):
        if not isinstance(raw_board, dict):
            warnings.append(f"Skipping non-dict board entry in overlay: {raw_board!r}")
            continue

        board_type = raw_board.get("type")
        if not board_type:
            warnings.append("Skipping overlay board with no 'type' field")
            continue

        if warn_unknown and board_type not in known_slugs:
            suggestions = sorted(known_slugs)
            warnings.append(
                f"Board type '{board_type}' not found in BSP registry. "
                f"Known device slugs: {', '.join(suggestions) if suggestions else '(none)'}"
            )

        # Work on a copy so we don't mutate caller data
        board = dict(raw_board)

        # Always increment the usage counter for this type
        type_counters[board_type] = type_counters.get(board_type, 0) + 1

        # Auto-generate name if absent, using the current counter value
        if "name" not in board:
            board["name"] = f"{board_type}-{type_counters[board_type]:02d}"

        # Apply default slave if not set per-board
        if "slave" not in board:
            board["slave"] = default_slave

        result_boards.append(board)

    return result_boards, warnings


# ---------------------------------------------------------------------------
# Main import orchestration
# ---------------------------------------------------------------------------

def import_boards_from_registry(
    registry_path=None,
    remote=None,
    branch=None,
    no_update=False,
    overlay_path=None,
    filter_devices=None,
):
    """
    Orchestrate a full BSP registry import.

    Args:
        registry_path:   path to a local bsp-registry.yaml (skips bsp tool)
        remote:          git URL for remote registry (passed to bsp tool)
        branch:          branch for remote registry
        no_update:       if True, skip updating the cached registry clone
        overlay_path:    path to overlay YAML with lab-specific wiring
        filter_devices:  optional list of device slugs to restrict import to

    Returns:
        (boards, warnings, registry_devices) tuple where:
            boards           – list of lava-docker board dicts ready for YAML output
            warnings         – list of warning message strings (may be empty)
            registry_devices – raw list of device dicts from the registry
    """
    # ---- Obtain device list from registry ----
    if registry_path:
        registry_devices = parse_bsp_registry_file(registry_path)
    else:
        raw_output = _run_bsp_list_devices(remote=remote, branch=branch, no_update=no_update)
        registry_devices = _parse_bsp_list_output(raw_output)

    if not registry_devices:
        raise ValueError(
            "No devices found in the BSP registry.\n"
            "Verify the registry file content or network connectivity."
        )

    # ---- Optional device filter ----
    if filter_devices:
        filter_set = set(filter_devices)
        registry_devices = [d for d in registry_devices if d["slug"] in filter_set]
        if not registry_devices:
            raise ValueError(
                f"None of the requested devices {list(filter_devices)} were found "
                "in the BSP registry."
            )

    # ---- Build overlay ----
    if overlay_path:
        overlay = load_overlay(overlay_path)
    else:
        # Without overlay: generate one placeholder board entry per device
        overlay = {
            "boards": [{"type": dev["slug"]} for dev in registry_devices]
        }

    # ---- Merge and return ----
    boards, warnings = merge_boards(registry_devices, overlay, warn_unknown=True)
    return boards, warnings, registry_devices


def write_boards_yaml(boards, output_path):
    """
    Write a minimal boards-only YAML fragment to output_path.
    Only the 'boards' section is written; masters/slaves must be provided
    separately in the full boards.yaml.
    """
    data = {"boards": boards}
    with open(output_path, "w") as fh:
        yaml.dump(data, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)
    print(f"Wrote {len(boards)} board(s) to '{output_path}'.")


# ---------------------------------------------------------------------------
# CLI entry point (called from lavalab-gen.py --import-bsp)
# ---------------------------------------------------------------------------

def build_import_parser():
    """Build and return the argparse parser for import mode."""
    parser = argparse.ArgumentParser(
        prog="lavalab-gen.py --import-bsp",
        description=(
            "Import board definitions from a BSP registry into a lava-docker "
            "boards YAML file.  Lab-specific hardware wiring (UART, PDU, slave "
            "assignment) is supplied via a local overlay file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Parse local registry + overlay, write boards-imported.yaml
  ./lavalab-gen.py --import-bsp --bsp-registry bsp-registry.yaml \\
      --overlay boards-overlay.yaml

  # Fetch default Advantech registry via bsp tool + overlay
  ./lavalab-gen.py --import-bsp --overlay boards-overlay.yaml

  # Use a specific remote + branch, skip update (e.g. in CI)
  ./lavalab-gen.py --import-bsp \\
      --bsp-remote https://github.com/my-org/bsp-registry.git \\
      --bsp-branch dev --bsp-no-update \\
      --overlay boards-overlay.yaml --output-boards my-boards.yaml
""",
    )
    parser.add_argument(
        "--import-bsp",
        action="store_true",
        required=True,
        help="Switch lavalab-gen.py into BSP import mode.",
    )
    parser.add_argument(
        "--bsp-registry",
        metavar="FILE",
        help="Path to a local bsp-registry.yaml / bsp-registry.yml file.  "
             "When supplied the 'bsp' CLI tool is NOT required.",
    )
    parser.add_argument(
        "--bsp-remote",
        metavar="URL",
        help=f"Remote BSP registry git URL (default: {DEFAULT_REMOTE}).  "
             "Passed through to the 'bsp' CLI tool.",
    )
    parser.add_argument(
        "--bsp-branch",
        metavar="BRANCH",
        default=DEFAULT_BRANCH,
        help=f"Remote registry branch (default: {DEFAULT_BRANCH}).",
    )
    parser.add_argument(
        "--bsp-no-update",
        action="store_true",
        help="Skip updating the cached registry clone (useful offline or in CI).",
    )
    parser.add_argument(
        "--overlay",
        metavar="FILE",
        help="Path to a YAML overlay file with lab-specific board wiring.  "
             "See boards-overlay.yaml.example for the expected format.  "
             "When omitted, one placeholder entry is generated per device.",
    )
    parser.add_argument(
        "--output-boards",
        metavar="FILE",
        default=DEFAULT_OUTPUT,
        help=f"Destination boards YAML file (default: {DEFAULT_OUTPUT}).",
    )
    return parser


def run_import_mode(argv=None):
    """
    Entry point called by lavalab-gen.py when '--import-bsp' is detected.

    argv: list of command-line arguments (default: sys.argv).
    Exits non-zero on error.
    """
    if argv is None:
        argv = sys.argv

    parser = build_import_parser()
    args = parser.parse_args(argv[1:])

    try:
        boards, warnings, registry_devices = import_boards_from_registry(
            registry_path=args.bsp_registry,
            remote=args.bsp_remote,
            branch=args.bsp_branch,
            no_update=args.bsp_no_update,
            overlay_path=args.overlay,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    for w in warnings:
        print(f"WARNING: {w}", file=sys.stderr)

    write_boards_yaml(boards, args.output_boards)

    if warnings:
        print(
            f"\n{len(warnings)} warning(s) issued. "
            "Review the output and correct any unrecognised board types.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    run_import_mode()
