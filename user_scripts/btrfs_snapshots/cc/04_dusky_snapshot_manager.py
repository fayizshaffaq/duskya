#!/usr/bin/env python3
"""
Advanced Btrfs/Snapper Flat Layout Manager (snapctl)
Designed for Arch Linux flat Btrfs topologies.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import typing as t
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


def run_cmd(cmd: list[str], check: bool = True) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if check and result.returncode != 0:
        print(f"[!] Command failed: {' '.join(cmd)}\n{result.stderr}", file=sys.stderr)
        sys.exit(result.returncode)
    return result.stdout.strip()

def get_btrfs_device(mountpoint: str) -> str:
    cmd = ["findmnt", "--fstab", "--evaluate", "-n", "-o", "SOURCE", mountpoint]
    device = run_cmd(cmd)
    if not device.startswith("/dev/"):
        sys.exit(f"[!] Fatal: Could not resolve physical block device for {mountpoint}. Found: {device}")
    return device

def get_subvol_from_fstab(mountpoint: str) -> str:
    options = run_cmd(["findmnt", "--fstab", "-n", "-o", "OPTIONS", mountpoint])
    match = re.search(r"subvol=([^,]+)", options)
    if not match:
        sys.exit(f"[!] Fatal: No 'subvol=' option found in fstab for {mountpoint}.")
    return match.group(1).lstrip("/")

def validate_snapshot_id(snap_id: str) -> str:
    if not snap_id.isdigit():
        sys.exit(f"[!] Fatal: Invalid snapshot ID: {snap_id!r}")
    return snap_id

@contextmanager
def mount_top_level(device: str) -> t.Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="btrfs_top_level_mgmt_", dir="/mnt") as tmpdir:
        mnt_point = Path(tmpdir)
        mounted = False
        print(f"[*] Mounting top-level tree (subvolid=5) for {device}...", file=sys.stderr)
        run_cmd(["mount", "-o", "subvolid=5", device, str(mnt_point)])
        mounted = True
        try:
            yield mnt_point
        finally:
            if mounted:
                print("[*] Unmounting top-level tree...", file=sys.stderr)
                run_cmd(["umount", str(mnt_point)])

def handle_list(config: str, as_json: bool) -> None:
    if not as_json:
        result = subprocess.run(["snapper", "-c", config, "list"])
        sys.exit(result.returncode)

    # Use check=False to prevent crashes if snapper config doesn't exist
    result = subprocess.run(
        ["snapper", "-c", config, "list", "--disable-used-space"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print("[]") # Gracefully return empty JSON for GUI to handle
        return

    raw_list = result.stdout.strip().splitlines()[2:]
    gui_data = []
    
    for line in raw_list:
        parts = [p.strip() for p in re.split(r'[|│]', line)]
        
        if len(parts) >= 7:
            snap_id = parts[0]
            if snap_id == "0" or not snap_id.isdigit():
                continue

            raw_date_str = parts[3]
            formatted_date = raw_date_str
            
            try:
                # Parse "Sun 22 Mar 2026 01:53:16 PM IST" to "03/22/26 01:53 PM"
                # Strip timezone abbreviation to avoid Python strptime errors
                date_tokens = raw_date_str.split()
                if len(date_tokens) >= 7:
                    clean_date = " ".join(date_tokens[:-1])
                    dt_obj = datetime.strptime(clean_date, "%a %d %b %Y %I:%M:%S %p")
                    formatted_date = dt_obj.strftime("%m/%d/%y %I:%M %p")
            except Exception:
                pass

            gui_data.append({
                "id": snap_id,
                "type": parts[1],
                "date": formatted_date,
                "raw_date": raw_date_str, # Kept for the bash wrapper to match against
                "description": parts[6]
            })
            
    print(json.dumps(gui_data))

def handle_create(config: str, description: str) -> None:
    print(f"[*] Creating snapshot for '{config}': {description}")
    run_cmd(["snapper", "-c", config, "create", "-d", description])
    print("[+] Snapshot created successfully.")

def handle_restore(config: str, snap_id: str) -> None:
    snap_id = validate_snapshot_id(snap_id)
    config_out = run_cmd(["snapper", "-c", config, "get-config"])
    target_mnt = ""
    for line in config_out.splitlines():
        if line.startswith("SUBVOLUME"):
            target_mnt = line.split("|")[-1].strip()
            break

    if not target_mnt:
        sys.exit(f"[!] Fatal: Could not determine SUBVOLUME for snapper config '{config}'.")

    snapshots_mnt = f"{target_mnt}/.snapshots" if target_mnt != "/" else "/.snapshots"
    device = get_btrfs_device(target_mnt)
    active_subvol = get_subvol_from_fstab(target_mnt)
    snapshots_subvol = get_subvol_from_fstab(snapshots_mnt)

    with mount_top_level(device) as top_mnt:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        source_snapshot = top_mnt / snapshots_subvol / snap_id / "snapshot"
        target_path = top_mnt / active_subvol
        backup_path = top_mnt / f"{active_subvol}_backup_{timestamp}"
        staging_path = top_mnt / f"{active_subvol}_restore_{snap_id}_{timestamp}"

        if not source_snapshot.is_dir():
            sys.exit(f"[!] Fatal: Snapshot ID {snap_id} does not exist at {source_snapshot}")

        nested_result = subprocess.run(["btrfs", "subvolume", "list", "-o", str(target_path)], capture_output=True, text=True)
        if nested_result.returncode != 0:
            sys.exit(f"[!] Fatal: Failed to inspect nested subvolumes inside '{active_subvol}'.")

        nested_check = nested_result.stdout.strip()
        if nested_check:
            print(f"\n[!] CRITICAL HALT: Nested subvolumes detected inside '{active_subvol}'!", file=sys.stderr)
            sys.exit(1)

        print(f"[*] Creating staged restore subvolume {staging_path.name}...")
        run_cmd(["btrfs", "subvolume", "snapshot", str(source_snapshot), str(staging_path)])

        moved_active = False
        try:
            print(f"[*] Moving active subvolume to {backup_path.name}...")
            target_path.rename(backup_path)
            moved_active = True
            print(f"[*] Activating restored snapshot as {target_path.name}...")
            staging_path.rename(target_path)
        except OSError as exc:
            if moved_active and not target_path.exists() and backup_path.exists():
                try:
                    backup_path.rename(target_path)
                    if staging_path.exists(): run_cmd(["btrfs", "subvolume", "delete", str(staging_path)], check=False)
                except OSError as rollback_exc:
                    sys.exit(f"[!] Fatal: Restore and rollback failed.\n{exc}\n{rollback_exc}")
                sys.exit(f"[!] Fatal: Restore failed. Rolled back successfully.\n{exc}")
            if not moved_active and staging_path.exists(): run_cmd(["btrfs", "subvolume", "delete", str(staging_path)], check=False)
            sys.exit(f"[!] Fatal: Restore failed.\n{exc}")

    print("\n[+] Restoration complete.")
    if target_mnt == "/":
        print("\n[!] ROOT FILESYSTEM RESTORED. You MUST reboot immediately for changes to take effect.")
    else:
        print(f"[*] Hot-reloading {target_mnt}...")
        run_cmd(["umount", "-l", target_mnt], check=False)
        run_cmd(["mount", target_mnt])
        print(f"[+] {target_mnt} successfully remounted.")

def main() -> None:
    if os.geteuid() != 0:
        sys.exit("[!] This script requires root privileges. Please run with sudo.")

    parser = argparse.ArgumentParser(description="Advanced Snapper Flat-Layout Manager for Arch Linux", formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("-c", "--config", required=True, help="Target Snapper configuration")
    parser.add_argument("--json", action="store_true", help="Format list output as JSON for GUI ingestion")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("-l", "--list", action="store_true", help="List snapshots for the configuration")
    group.add_argument("-C", "--create", metavar="DESC", help="Create a new snapshot with a description")
    group.add_argument("-R", "--restore", metavar="ID", help="Restore subvolume to the specified snapshot ID")

    args = parser.parse_args()
    match args:
        case args if args.list: handle_list(args.config, args.json)
        case args if args.create: handle_create(args.config, args.create)
        case args if args.restore: handle_restore(args.config, args.restore)

if __name__ == "__main__":
    main()
