#!/usr/bin/env python3
"""Route application audio output to a virtual microphone via PipeWire.

Architecture : Arch Linux / Wayland / Hyprland
Ecosystem    : PipeWire 1.4+ / WirePlumber
Python       : 3.14+

Usage:
    audio_router.py                     # GUI popup (control / start)
    audio_router.py --daemon [APP]      # background routing daemon
    audio_router.py --status            # print daemon status
    audio_router.py --stop              # stop running daemon
    audio_router.py --waybar            # output Waybar JSON status
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────

VIRT_NODE_NAME = "Virtual_Mic_Tx"
SOCK_NAME = "audio_router.sock"
DEFAULT_APP = "mpv"
LINK_TIMEOUT = 2.0
POLL_INTERVAL = 0.5
INIT_LINK_MAX_AGE = 3.0
MAX_LINK_RETRIES = 3
MAX_CONCURRENT_LINKS = 4
MODULE_WAIT_TIMEOUT = 5.0
MODULE_WAIT_STEP = 0.1

APP_MATCH_KEYS: tuple[str, ...] = (
    "application.name",
    "application.process.binary",
    "node.name",
)

type PortPair = tuple[int, int]
type ChannelMap = dict[str, int]


# ──────────────────────────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class StreamInfo:
    node_id: int
    state: str  # running | idle | suspended | error
    app_name: str
    media_name: str
    ports: ChannelMap  # {"FL": port_id, "FR": port_id}


@dataclass(slots=True)
class TrackedLink:
    out_port: int
    in_port: int
    node_id: int
    created_at: float
    last_state: str = "unknown"


@dataclass
class DaemonState:
    target_app: str = DEFAULT_APP
    virt_module_id: str | None = None
    virt_node_id: int | None = None
    virt_ports: ChannelMap = field(default_factory=dict)
    our_links: dict[PortPair, TrackedLink] = field(default_factory=dict)
    prev_ports: dict[int, ChannelMap] = field(default_factory=dict)
    failed_pairs: dict[PortPair, int] = field(default_factory=dict)
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class GraphSnapshot:
    streams: dict[int, StreamInfo]
    running_ids: set[int]
    virt_node_id: int | None
    virt_ports: ChannelMap
    graph_links: dict[PortPair, str]  # (out, in) → state string


# ──────────────────────────────────────────────────────────────────
# Socket path helper
# ──────────────────────────────────────────────────────────────────

def _sock_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return Path(runtime) / SOCK_NAME


# ──────────────────────────────────────────────────────────────────
# PipeWire graph parsing
# ──────────────────────────────────────────────────────────────────

async def async_pw_dump() -> list[dict]:
    """Capture the live PipeWire object graph asynchronously."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pw-dump",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        return json.loads(stdout.decode())
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError) as exc:
        print(f"Warning: pw-dump failed: {exc}", file=sys.stderr)
        return []


def sync_pw_dump() -> list[dict]:
    """Synchronous pw-dump for non-async contexts (GUI startup)."""
    try:
        out = subprocess.check_output(
            ["pw-dump"], text=True, timeout=5,
            stderr=subprocess.DEVNULL,
        )
        return json.loads(out)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            json.JSONDecodeError, OSError):
        return []


def _extract_channel(props: dict) -> str:
    ch = props.get("audio.channel")
    if isinstance(ch, str) and ch:
        return ch.upper()
    name = props.get("port.name", "")
    if isinstance(name, str):
        up = name.upper()
        if "FL" in up:
            return "FL"
        if "FR" in up:
            return "FR"
    return ""


def _matches_target(props: dict, target: str) -> bool:
    """Case-insensitive exact match on known application identity keys."""
    target_lower = target.lower()
    for key in APP_MATCH_KEYS:
        val = props.get(key)
        if isinstance(val, str) and val.lower() == target_lower:
            return True
    return False


def _node_state(obj: dict) -> str:
    """Extract the node state string from a pw-dump object."""
    state_raw = obj.get("info", {}).get("state")
    if isinstance(state_raw, str):
        return state_raw.lower()
    # pw-dump may also encode state under info.props
    return obj.get("info", {}).get("props", {}).get("node.state", "unknown").lower()


def build_snapshot(graph: list[dict], target: str) -> GraphSnapshot:
    """Parse a pw-dump graph into a structured snapshot."""
    streams: dict[int, StreamInfo] = {}
    virt_node_id: int | None = None
    virt_ports: ChannelMap = {}
    node_ports: dict[int, list[tuple[str, int, str]]] = {}  # node_id → [(ch, port_id, direction)]
    graph_links: dict[PortPair, str] = {}

    # Pass 1: identify nodes
    for obj in graph:
        match obj.get("type"):
            case "PipeWire:Interface:Node":
                props = obj.get("info", {}).get("props", {})
                nid = obj.get("id")
                if nid is None:
                    continue
                if props.get("node.name") == VIRT_NODE_NAME:
                    virt_node_id = nid
                elif props.get("media.class") == "Stream/Output/Audio":
                    if _matches_target(props, target):
                        state = _node_state(obj)
                        streams[nid] = StreamInfo(
                            node_id=nid,
                            state=state,
                            app_name=props.get("application.name", "unknown"),
                            media_name=props.get("media.name", ""),
                            ports={},
                        )

    # Pass 2: collect ports
    for obj in graph:
        if obj.get("type") != "PipeWire:Interface:Port":
            continue
        info = obj.get("info", {})
        props = info.get("props", {})
        raw_parent = props.get("node.id")
        if raw_parent is None:
            continue
        try:
            parent = int(raw_parent)
        except (ValueError, TypeError):
            continue
        direction = str(info.get("direction", "")).lower()
        channel = _extract_channel(props)
        port_id = obj.get("id")
        if port_id is None:
            continue

        if parent == virt_node_id and direction == "input" and channel:
            virt_ports[channel] = port_id
        elif parent in streams and direction == "output" and channel:
            streams[parent].ports[channel] = port_id

    # Pass 3: collect links
    for obj in graph:
        if obj.get("type") != "PipeWire:Interface:Link":
            continue
        info = obj.get("info", {})
        out_p = info.get("output-port-id")
        in_p = info.get("input-port-id")
        if out_p is None or in_p is None:
            continue
        try:
            pair = (int(out_p), int(in_p))
        except (ValueError, TypeError):
            continue
        state = str(info.get("state", "unknown")).lower()
        graph_links[pair] = state

    running_ids = {nid for nid, s in streams.items() if s.state == "running"}

    return GraphSnapshot(
        streams=streams,
        running_ids=running_ids,
        virt_node_id=virt_node_id,
        virt_ports=virt_ports,
        graph_links=graph_links,
    )


def discover_audio_apps(graph: list[dict]) -> list[str]:
    """Find all unique application names with active audio output streams."""
    apps: set[str] = set()
    for obj in graph:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = obj.get("info", {}).get("props", {})
        if props.get("media.class") != "Stream/Output/Audio":
            continue
        name = props.get("application.name")
        if isinstance(name, str) and name:
            apps.add(name)
    return sorted(apps, key=str.lower)


# ──────────────────────────────────────────────────────────────────
# Link management
# ──────────────────────────────────────────────────────────────────

async def create_link(out_port: int, in_port: int) -> str:
    """Create a pw-link. Returns error string (empty on success)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pw-link", str(out_port), str(in_port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=LINK_TIMEOUT,
        )
        err = stderr.decode().strip() if stderr else ""
        # "File exists" means link already exists — not an error
        if "file exists" in err.lower():
            return ""
        return err if proc.returncode != 0 else ""
    except asyncio.TimeoutError:
        return "timeout"
    except OSError as exc:
        return str(exc)


async def destroy_link(out_port: int, in_port: int) -> None:
    """Destroy a link between two ports."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pw-link", "-d", str(out_port), str(in_port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=LINK_TIMEOUT)
    except (asyncio.TimeoutError, OSError):
        pass


def compute_desired_links(
    snap: GraphSnapshot, target_ids: set[int],
) -> dict[PortPair, int]:
    """Compute desired links: {(out_port, in_port): node_id} for running targets."""
    desired: dict[PortPair, int] = {}
    vp = snap.virt_ports
    if not vp:
        return desired

    for nid in target_ids:
        stream = snap.streams.get(nid)
        if stream is None:
            continue
        tp = stream.ports
        if not tp:
            continue

        if len(tp) == 1:
            # Mono: fan out to all virtual mic inputs
            only_port = next(iter(tp.values()))
            for v_port in vp.values():
                desired[(only_port, v_port)] = nid
        else:
            # Stereo: channel-matched
            for ch in ("FL", "FR"):
                if ch in tp and ch in vp:
                    desired[(tp[ch], vp[ch])] = nid

    return desired


# ──────────────────────────────────────────────────────────────────
# Virtual mic setup / teardown
# ──────────────────────────────────────────────────────────────────

def cleanup_stale_modules() -> bool:
    """Remove orphaned Virtual_Mic_Tx modules from prior runs."""
    try:
        out = subprocess.check_output(
            ["pactl", "list", "modules", "short"],
            text=True, timeout=5,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False

    removed = False
    for line in out.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        mod_id, mod_name, args = fields[0], fields[1], fields[2]
        if mod_name == "module-null-sink" and VIRT_NODE_NAME in args:
            print(f":: Removing stale module (ID: {mod_id})")
            with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                subprocess.run(
                    ["pactl", "unload-module", mod_id],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            removed = True
    return removed


def create_virtual_mic() -> str:
    """Create the virtual mic module, return module ID."""
    try:
        out = subprocess.check_output(
            [
                "pactl", "load-module", "module-null-sink",
                "media.class=Audio/Source/Virtual",
                f"sink_name={VIRT_NODE_NAME}",
                "channel_map=front-left,front-right",
                "format=float32le",
                "rate=48000",
            ],
            text=True, timeout=10,
            stderr=subprocess.PIPE,
        )
        return out.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        print(f"Fatal: Failed to create virtual mic: {exc}", file=sys.stderr)
        sys.exit(1)


def destroy_virtual_mic(module_id: str | None) -> None:
    """Unload the virtual mic module."""
    if not module_id:
        return
    print(f":: Tearing down virtual mic (Module ID: {module_id})")
    with contextlib.suppress(subprocess.TimeoutExpired, OSError,
                             subprocess.CalledProcessError):
        subprocess.run(
            ["pactl", "unload-module", module_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )


async def wait_for_virtual_mic(target: str) -> tuple[int, ChannelMap]:
    """Wait for the virtual mic node and its input ports to appear."""
    deadline = time.monotonic() + MODULE_WAIT_TIMEOUT
    while time.monotonic() < deadline:
        graph = await async_pw_dump()
        if not graph:
            await asyncio.sleep(MODULE_WAIT_STEP)
            continue
        snap = build_snapshot(graph, target)
        if snap.virt_node_id is not None and len(snap.virt_ports) >= 2:
            return snap.virt_node_id, snap.virt_ports
        await asyncio.sleep(MODULE_WAIT_STEP)
    print("Fatal: Virtual mic did not materialize.", file=sys.stderr)
    sys.exit(1)


# ──────────────────────────────────────────────────────────────────
# Monitor loop
# ──────────────────────────────────────────────────────────────────

async def monitor_loop(ds: DaemonState) -> None:
    """Main routing loop — runs until shutdown_event is set."""
    print(f":: Monitoring for '{ds.target_app}' streams. Ctrl+C to stop.\n")
    prev_running: set[int] = set()
    prev_all: set[int] = set()

    while not ds.shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                ds.shutdown_event.wait(), timeout=POLL_INTERVAL,
            )
            break  # shutdown requested
        except asyncio.TimeoutError:
            pass  # poll interval elapsed, do work

        graph = await async_pw_dump()
        if not graph:
            continue

        snap = build_snapshot(graph, ds.target_app)

        # ── Virtual mic sanity ────────────────────────────────
        if snap.virt_node_id is None:
            print("Warning: Virtual mic vanished from graph.", file=sys.stderr)
            continue
        ds.virt_node_id = snap.virt_node_id
        ds.virt_ports = snap.virt_ports

        all_ids = set(snap.streams)

        # ── Log stream changes ────────────────────────────────
        for nid in sorted(all_ids - prev_all):
            s = snap.streams[nid]
            label = f"'{s.media_name}'" if s.media_name else f"node {nid}"
            print(f":: Stream appeared: {s.app_name} — {label} ({s.state})")
        for nid in sorted(prev_all - all_ids):
            print(f":: Stream gone: node {nid}")

        for nid in sorted(snap.running_ids - prev_running):
            if nid in prev_all:
                s = snap.streams[nid]
                label = f"'{s.media_name}'" if s.media_name else f"node {nid}"
                print(f":: Stream activated: {s.app_name} — {label}")

        prev_all = all_ids
        prev_running = snap.running_ids.copy()

        # ── Detect port recycling ─────────────────────────────
        needs_relink: set[int] = set()
        for nid, stream in snap.streams.items():
            old_ports = ds.prev_ports.get(nid)
            if old_ports is not None and old_ports != stream.ports:
                # Ports changed — destroy stale links
                stale = [
                    pair for pair, tl in ds.our_links.items()
                    if tl.node_id == nid
                ]
                if stale:
                    print(f":: Port recycled on node {nid}: destroying {len(stale)} stale link(s)")
                    for pair in stale:
                        await destroy_link(*pair)
                        ds.our_links.pop(pair, None)
                        ds.failed_pairs.pop(pair, None)
                needs_relink.add(nid)
            ds.prev_ports[nid] = dict(stream.ports)

        # ── Detect disappeared nodes ──────────────────────────
        gone_nodes = set(ds.prev_ports) - all_ids
        for nid in gone_nodes:
            stale = [
                pair for pair, tl in ds.our_links.items()
                if tl.node_id == nid
            ]
            for pair in stale:
                await destroy_link(*pair)
                ds.our_links.pop(pair, None)
                ds.failed_pairs.pop(pair, None)
            ds.prev_ports.pop(nid, None)

        # ── Cleanup zombie links ──────────────────────────────
        now = time.monotonic()
        zombies: list[PortPair] = []
        for pair, tl in list(ds.our_links.items()):
            graph_state = snap.graph_links.get(pair)
            if graph_state is None:
                # PipeWire deleted it behind our back
                zombies.append(pair)
            elif graph_state == "error":
                zombies.append(pair)
            elif graph_state == "init" and (now - tl.created_at) > INIT_LINK_MAX_AGE:
                zombies.append(pair)
            else:
                tl.last_state = graph_state or "unknown"

        for pair in zombies:
            tl = ds.our_links.pop(pair, None)
            if tl and snap.graph_links.get(pair) is not None:
                # Still exists in graph — actively destroy it
                await destroy_link(*pair)
                needs_relink.add(tl.node_id)

        # ── Compute desired links (RUNNING nodes only) ────────
        linkable = (snap.running_ids | needs_relink) & all_ids
        # Only actually link nodes that are running right now
        linkable = {nid for nid in linkable
                    if snap.streams.get(nid, StreamInfo(0, "idle", "", "", {})).state == "running"}

        desired = compute_desired_links(snap, linkable)

        # ── Determine missing links ───────────────────────────
        existing_healthy = {
            pair for pair in ds.our_links
            if ds.our_links[pair].last_state in ("active", "paused")
        }
        missing = set(desired) - existing_healthy - set(ds.our_links)

        # Exclude pairs that have failed too many times
        missing = {
            pair for pair in missing
            if ds.failed_pairs.get(pair, 0) < MAX_LINK_RETRIES
        }

        # ── Create missing links (concurrent) ─────────────────
        if missing:
            sem = asyncio.Semaphore(MAX_CONCURRENT_LINKS)

            async def _do_link(pair: PortPair) -> tuple[PortPair, str]:
                async with sem:
                    err = await create_link(*pair)
                    return pair, err

            results = await asyncio.gather(
                *[_do_link(p) for p in missing],
                return_exceptions=True,
            )

            created = 0
            for result in results:
                if isinstance(result, BaseException):
                    continue
                pair, err = result
                nid = desired.get(pair, 0)
                if not err:
                    ds.our_links[pair] = TrackedLink(
                        out_port=pair[0],
                        in_port=pair[1],
                        node_id=nid,
                        created_at=time.monotonic(),
                        last_state="init",
                    )
                    ds.failed_pairs.pop(pair, None)
                    created += 1
                else:
                    count = ds.failed_pairs.get(pair, 0) + 1
                    ds.failed_pairs[pair] = count
                    if count >= MAX_LINK_RETRIES:
                        stream = snap.streams.get(nid)
                        name = stream.media_name if stream else f"node {nid}"
                        print(
                            f"Warning: Giving up on link {pair[0]}->{pair[1]} "
                            f"({name}): {err}",
                            file=sys.stderr,
                        )

            if created:
                print(
                    f":: Linked {created} port(s) across "
                    f"{len(linkable)} running stream(s) → '{VIRT_NODE_NAME}'"
                )

        # ── Reset failed_pairs for nodes whose ports recycled ─
        for nid in needs_relink:
            to_clear = [p for p, c in ds.failed_pairs.items()
                        if desired.get(p) == nid]
            for p in to_clear:
                ds.failed_pairs.pop(p, None)


# ──────────────────────────────────────────────────────────────────
# IPC server (daemon side)
# ──────────────────────────────────────────────────────────────────

async def ipc_handler(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ds: DaemonState,
    graph_fn,
) -> None:
    """Handle a single IPC client connection."""
    try:
        raw = await asyncio.wait_for(reader.readline(), timeout=5)
        if not raw:
            return
        request = json.loads(raw.decode())
        action = request.get("action", "")

        match action:
            case "ping":
                response = {"ok": True}

            case "status":
                graph = await graph_fn()
                snap = build_snapshot(graph, ds.target_app) if graph else None
                streams_info = []
                active_links = sum(
                    1 for tl in ds.our_links.values()
                    if tl.last_state in ("active", "paused")
                )
                if snap:
                    for nid, s in sorted(snap.streams.items()):
                        node_links = sum(
                            1 for tl in ds.our_links.values()
                            if tl.node_id == nid
                            and tl.last_state in ("active", "paused")
                        )
                        streams_info.append({
                            "node_id": nid,
                            "state": s.state,
                            "app_name": s.app_name,
                            "media_name": s.media_name,
                            "links": node_links,
                        })
                response = {
                    "ok": True,
                    "data": {
                        "target": ds.target_app,
                        "virt_node": VIRT_NODE_NAME,
                        "active_links": active_links,
                        "total_tracked": len(ds.our_links),
                        "streams": streams_info,
                    },
                }

            case "list_apps":
                graph = await graph_fn()
                apps = discover_audio_apps(graph) if graph else []
                response = {"ok": True, "apps": apps}

            case "set_target":
                new_target = request.get("app", "").strip()
                if not new_target:
                    response = {"ok": False, "error": "No app specified"}
                else:
                    # Destroy all current links
                    for pair in list(ds.our_links):
                        await destroy_link(*pair)
                    ds.our_links.clear()
                    ds.prev_ports.clear()
                    ds.failed_pairs.clear()
                    ds.target_app = new_target
                    print(f":: Target changed to '{new_target}'")
                    response = {"ok": True}

            case "stop":
                response = {"ok": True}
                ds.shutdown_event.set()

            case _:
                response = {"ok": False, "error": f"Unknown action: {action}"}

        writer.write(json.dumps(response).encode() + b"\n")
        await writer.drain()
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def start_ipc_server(ds: DaemonState) -> asyncio.Server | None:
    """Start the Unix socket IPC server."""
    sock_path = _sock_path()
    # Remove stale socket file
    with contextlib.suppress(OSError):
        sock_path.unlink()

    try:
        server = await asyncio.start_unix_server(
            lambda r, w: ipc_handler(r, w, ds, async_pw_dump),
            path=str(sock_path),
        )
        sock_path.chmod(0o600)
        return server
    except OSError as exc:
        print(f"Warning: Could not start IPC server: {exc}", file=sys.stderr)
        return None


# ──────────────────────────────────────────────────────────────────
# IPC client (GUI / CLI side)
# ──────────────────────────────────────────────────────────────────

def ipc_send(request: dict) -> dict | None:
    """Send a request to the daemon and return the response."""
    sock_path = _sock_path()
    if not sock_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.connect(str(sock_path))
            sock.sendall(json.dumps(request).encode() + b"\n")
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"\n" in data:
                    break
            return json.loads(data.decode())
    except (OSError, json.JSONDecodeError):
        return None


def daemon_is_running() -> bool:
    resp = ipc_send({"action": "ping"})
    return resp is not None and resp.get("ok") is True


# ──────────────────────────────────────────────────────────────────
# Daemon entry
# ──────────────────────────────────────────────────────────────────

async def daemon_main(target_app: str) -> None:
    """Async entry point for the routing daemon."""
    ds = DaemonState(target_app=target_app)

    print(f":: Initializing audio routing for [{target_app}]...")

    # Singleton check
    if daemon_is_running():
        print("Fatal: Another daemon instance is already running.", file=sys.stderr)
        print("  Use --stop to stop it, or run without flags for the GUI.", file=sys.stderr)
        sys.exit(1)

    # Cleanup stale modules
    if cleanup_stale_modules():
        await asyncio.sleep(0.5)

    # Create virtual mic
    ds.virt_module_id = create_virtual_mic()
    print(f":: Virtual mic created (Module ID: {ds.virt_module_id})")

    # Register cleanup
    def _cleanup():
        # Destroy tracked links
        for pair in list(ds.our_links):
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["pw-link", "-d", str(pair[0]), str(pair[1])],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                )
        destroy_virtual_mic(ds.virt_module_id)
        ds.virt_module_id = None
        sock_path = _sock_path()
        with contextlib.suppress(OSError):
            sock_path.unlink()

    atexit.register(_cleanup)

    def _signal_handler(signum, frame):
        ds.shutdown_event.set()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGHUP, _signal_handler)

    # Wait for virtual mic to appear
    virt_id, virt_ports = await wait_for_virtual_mic(target_app)
    ds.virt_node_id = virt_id
    ds.virt_ports = virt_ports
    print(f":: Virtual mic ready (Node ID: {virt_id}, Ports: {virt_ports})")

    # Start IPC server
    server = await start_ipc_server(ds)

    try:
        await monitor_loop(ds)
    finally:
        if server:
            server.close()
            await server.wait_closed()
        # Async cleanup of links
        for pair in list(ds.our_links):
            await destroy_link(*pair)
        ds.our_links.clear()


# ──────────────────────────────────────────────────────────────────
# GUI popup
# ──────────────────────────────────────────────────────────────────

def _try_libadwaita_popup(running: bool, status: dict | None, apps: list[str]) -> bool:
    """Attempt to show a GTK4/Libadwaita popup. Returns False if unavailable."""
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk, GLib
    except (ImportError, ValueError):
        return False

    result: dict = {"action": None, "app": None}

    app = Adw.Application(application_id="dev.audiorouter.popup")

    def on_activate(application: Adw.Application):
        win = Adw.Window(
            title="Audio Router",
            default_width=420,
            default_height=-1,
            application=application,
        )

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        win.set_content(box)

        header = Adw.HeaderBar()
        box.append(header)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        box.append(scrolled)

        clamp = Adw.Clamp(maximum_size=400)
        scrolled.set_child(clamp)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            margin_top=24,
            margin_bottom=24,
            margin_start=16,
            margin_end=16,
        )
        clamp.set_child(content)

        if running and status:
            # ── Daemon is running ──
            data = status.get("data", {})
            streams = data.get("streams", [])
            active = [s for s in streams if s.get("state") == "running"]
            idle = [s for s in streams if s.get("state") != "running"]

            # Status banner
            status_group = Adw.PreferencesGroup(title="Status")
            content.append(status_group)

            row_target = Adw.ActionRow(
                title="Target",
                subtitle=data.get("target", "?"),
            )
            status_group.add(row_target)

            row_streams = Adw.ActionRow(
                title="Streams",
                subtitle=f"{len(active)} active / {len(idle)} idle",
            )
            status_group.add(row_streams)

            row_links = Adw.ActionRow(
                title="Links",
                subtitle=f"{data.get('active_links', 0)} active",
            )
            status_group.add(row_links)

            # Active streams list
            if active:
                stream_group = Adw.PreferencesGroup(title="Active Streams")
                content.append(stream_group)
                for s in active:
                    name = s.get("media_name") or f"Node {s.get('node_id')}"
                    row = Adw.ActionRow(
                        title=name,
                        subtitle=f"Node {s.get('node_id')} · {s.get('links', 0)} link(s)",
                    )
                    row.add_prefix(
                        Gtk.Image.new_from_icon_name("audio-volume-high-symbolic")
                    )
                    stream_group.add(row)

            # Change target combo
            if apps:
                target_group = Adw.PreferencesGroup(title="Change Target")
                content.append(target_group)

                string_list = Gtk.StringList()
                current_idx = 0
                for i, a in enumerate(apps):
                    string_list.append(a)
                    if a.lower() == data.get("target", "").lower():
                        current_idx = i

                combo_row = Adw.ComboRow(
                    title="Application",
                    model=string_list,
                )
                combo_row.set_selected(current_idx)
                target_group.add(combo_row)

                def on_apply(_btn):
                    idx = combo_row.get_selected()
                    selected = string_list.get_string(idx)
                    if selected and selected.lower() != data.get("target", "").lower():
                        result["action"] = "set_target"
                        result["app"] = selected
                    win.close()

                def on_stop(_btn):
                    result["action"] = "stop"
                    win.close()

                btn_box = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    spacing=12,
                    halign=Gtk.Align.END,
                    margin_top=8,
                )
                content.append(btn_box)

                stop_btn = Gtk.Button(label="Stop Daemon")
                stop_btn.add_css_class("destructive-action")
                stop_btn.connect("clicked", on_stop)
                btn_box.append(stop_btn)

                apply_btn = Gtk.Button(label="Apply & Close")
                apply_btn.add_css_class("suggested-action")
                apply_btn.connect("clicked", on_apply)
                btn_box.append(apply_btn)
            else:
                # No apps but daemon running — just offer stop
                def on_stop(_btn):
                    result["action"] = "stop"
                    win.close()

                btn_box = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    halign=Gtk.Align.END,
                    margin_top=8,
                )
                content.append(btn_box)
                stop_btn = Gtk.Button(label="Stop Daemon")
                stop_btn.add_css_class("destructive-action")
                stop_btn.connect("clicked", on_stop)
                btn_box.append(stop_btn)

        else:
            # ── Daemon not running ──
            info_label = Gtk.Label(
                label="No routing daemon is running.",
                css_classes=["dim-label"],
                margin_bottom=8,
            )
            content.append(info_label)

            if apps:
                start_group = Adw.PreferencesGroup(title="Start Routing")
                content.append(start_group)

                string_list = Gtk.StringList()
                for a in apps:
                    string_list.append(a)

                combo_row = Adw.ComboRow(
                    title="Target Application",
                    model=string_list,
                )
                start_group.add(combo_row)

                def on_start(_btn):
                    idx = combo_row.get_selected()
                    selected = string_list.get_string(idx)
                    if selected:
                        result["action"] = "start"
                        result["app"] = selected
                    win.close()

                def on_cancel(_btn):
                    win.close()

                btn_box = Gtk.Box(
                    orientation=Gtk.Orientation.HORIZONTAL,
                    spacing=12,
                    halign=Gtk.Align.END,
                    margin_top=8,
                )
                content.append(btn_box)

                cancel_btn = Gtk.Button(label="Cancel")
                cancel_btn.connect("clicked", on_cancel)
                btn_box.append(cancel_btn)

                start_btn = Gtk.Button(label="Start")
                start_btn.add_css_class("suggested-action")
                start_btn.connect("clicked", on_start)
                btn_box.append(start_btn)
            else:
                no_app_label = Gtk.Label(
                    label="No audio applications detected.",
                    css_classes=["dim-label"],
                )
                content.append(no_app_label)

        win.present()

    app.connect("activate", on_activate)
    app.run(None)

    # Process result
    match result["action"]:
        case "stop":
            ipc_send({"action": "stop"})
            print(":: Daemon stop requested.")
        case "set_target" if result["app"]:
            ipc_send({"action": "set_target", "app": result["app"]})
            print(f":: Target changed to '{result['app']}'.")
        case "start" if result["app"]:
            _spawn_daemon(result["app"])
            print(f":: Daemon started for '{result['app']}'.")

    return True


def _try_yad_popup(running: bool, status: dict | None, apps: list[str]) -> bool:
    """Fallback: yad-based popup."""
    import shutil
    if not shutil.which("yad"):
        return False

    if running and status:
        data = status.get("data", {})
        streams = data.get("streams", [])
        active = [s for s in streams if s.get("state") == "running"]

        stream_text = "\n".join(
            f"  {s.get('media_name') or 'Node ' + str(s.get('node_id'))} ({s.get('links', 0)} links)"
            for s in active
        ) or "  (none)"

        info_text = (
            f"Target: {data.get('target', '?')}\n"
            f"Active streams: {len(active)}\n"
            f"Active links: {data.get('active_links', 0)}\n\n"
            f"Streams:\n{stream_text}"
        )

        app_options = "!".join(apps) if apps else data.get("target", DEFAULT_APP)

        try:
            out = subprocess.check_output(
                [
                    "yad", "--form",
                    "--title=Audio Router",
                    "--width=400",
                    "--text", info_text,
                    "--field=Target:CB", app_options,
                    "--field=Stop Daemon:BTN", "bash -c 'echo STOP'",
                    "--button=Apply:0",
                    "--button=Cancel:1",
                ],
                text=True, timeout=60,
            )
            parts = out.strip().split("|")
            if parts:
                selected = parts[0].strip()
                if selected and selected.lower() != data.get("target", "").lower():
                    ipc_send({"action": "set_target", "app": selected})
                    print(f":: Target changed to '{selected}'.")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        return True

    else:
        # Not running
        if not apps:
            with contextlib.suppress(Exception):
                subprocess.run(
                    ["yad", "--info", "--title=Audio Router",
                     "--text=No audio applications detected.", "--button=OK:0"],
                    timeout=30,
                )
            return True

        app_options = "!".join(apps)
        try:
            out = subprocess.check_output(
                [
                    "yad", "--form",
                    "--title=Audio Router — Start",
                    "--width=400",
                    "--text=No daemon running. Select target to start:",
                    "--field=Application:CB", app_options,
                    "--button=Start:0",
                    "--button=Cancel:1",
                ],
                text=True, timeout=60,
            )
            selected = out.strip().split("|")[0].strip()
            if selected:
                _spawn_daemon(selected)
                print(f":: Daemon started for '{selected}'.")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        return True


def _try_terminal_menu(running: bool, status: dict | None, apps: list[str]) -> bool:
    """Last resort: terminal-based menu."""
    if running and status:
        data = status.get("data", {})
        streams = data.get("streams", [])
        active = [s for s in streams if s.get("state") == "running"]

        print("\n╔══ Audio Router ═══════════════════════╗")
        print(f"║  Target : {data.get('target', '?'):<28}║")
        print(f"║  Links  : {data.get('active_links', 0)} active{' ' * 21}║")
        print(f"║  Streams: {len(active)} running / {len(streams) - len(active)} idle{' ' * (13 - len(str(len(active))) - len(str(len(streams) - len(active))))}║")
        print("╠═══════════════════════════════════════╣")
        print("║  1. Change target                     ║")
        print("║  2. Stop daemon                       ║")
        print("║  3. Cancel                            ║")
        print("╚═══════════════════════════════════════╝")

        try:
            choice = input("\nChoice: ").strip()
        except (EOFError, KeyboardInterrupt):
            return True

        if choice == "1" and apps:
            print("\nAvailable apps:")
            for i, a in enumerate(apps, 1):
                print(f"  {i}. {a}")
            try:
                idx = int(input("Select: ").strip()) - 1
                if 0 <= idx < len(apps):
                    ipc_send({"action": "set_target", "app": apps[idx]})
                    print(f":: Target changed to '{apps[idx]}'.")
            except (ValueError, EOFError, KeyboardInterrupt):
                pass
        elif choice == "2":
            ipc_send({"action": "stop"})
            print(":: Daemon stopped.")
    else:
        if not apps:
            print("No audio applications detected.")
            return True

        print("\n╔══ Audio Router — Start ════════════════╗")
        print("║  No daemon running.                    ║")
        print("║  Select target application:            ║")
        print("╠════════════════════════════════════════╣")
        for i, a in enumerate(apps, 1):
            print(f"║  {i}. {a:<36}║")
        print("║  0. Cancel                             ║")
        print("╚════════════════════════════════════════╝")

        try:
            idx = int(input("\nSelect: ").strip())
            if 1 <= idx <= len(apps):
                _spawn_daemon(apps[idx - 1])
                print(f":: Daemon started for '{apps[idx - 1]}'.")
        except (ValueError, EOFError, KeyboardInterrupt):
            pass

    return True


def _spawn_daemon(target: str) -> None:
    """Spawn the daemon as a detached background process."""
    subprocess.Popen(
        [sys.executable, __file__, "--daemon", target],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def gui_main() -> None:
    """Entry point for the GUI popup."""
    running = daemon_is_running()
    status: dict | None = None
    apps: list[str] = []

    if running:
        resp = ipc_send({"action": "status"})
        status = resp if resp and resp.get("ok") else None
        resp2 = ipc_send({"action": "list_apps"})
        apps = resp2.get("apps", []) if resp2 and resp2.get("ok") else []
    else:
        graph = sync_pw_dump()
        apps = discover_audio_apps(graph) if graph else []

    # Try each GUI toolkit in order
    if _try_libadwaita_popup(running, status, apps):
        return
    if _try_yad_popup(running, status, apps):
        return
    _try_terminal_menu(running, status, apps)


# ──────────────────────────────────────────────────────────────────
# CLI commands
# ──────────────────────────────────────────────────────────────────

def cli_status() -> None:
    """Print daemon status to stdout."""
    if not daemon_is_running():
        print("Daemon is not running.")
        sys.exit(1)

    resp = ipc_send({"action": "status"})
    if not resp or not resp.get("ok"):
        print("Failed to get status.", file=sys.stderr)
        sys.exit(1)

    data = resp["data"]
    streams = data.get("streams", [])
    active = [s for s in streams if s.get("state") == "running"]

    print(f"Target   : {data['target']}")
    print(f"Virtual  : {data['virt_node']}")
    print(f"Links    : {data['active_links']} active / {data['total_tracked']} tracked")
    print(f"Streams  : {len(active)} running / {len(streams)} total")
    if active:
        print("\nActive streams:")
        for s in active:
            name = s.get("media_name") or f"(unnamed)"
            print(f"  Node {s['node_id']:>4} — {name} ({s.get('links', 0)} links)")


def cli_stop() -> None:
    """Tell the daemon to shut down."""
    if not daemon_is_running():
        print("Daemon is not running.")
        return
    resp = ipc_send({"action": "stop"})
    if resp and resp.get("ok"):
        print(":: Daemon stopping.")
    else:
        print("Failed to stop daemon.", file=sys.stderr)
        sys.exit(1)


def cli_waybar() -> None:
    """Output JSON formatted for a Waybar custom module."""
    payload = {}
    
    if not daemon_is_running():
        payload = {
            "text": "󰍭",
            "tooltip": "Routing daemon stopped",
            "class": "inactive"
        }
    else:
        resp = ipc_send({"action": "status"})
        if resp and resp.get("ok"):
            data = resp.get("data", {})
            target = data.get("target", "Unknown")
            links = data.get("active_links", 0)
            payload = {
                "text": "󰍬",
                "tooltip": f"Routing: {target}\nActive Links: {links}",
                "class": "active"
            }
        else:
            # Fallback if IPC fails but daemon exists
            payload = {
                "text": "󰍬",
                "tooltip": "Routing daemon running (status unknown)",
                "class": "active"
            }
            
    # ensure_ascii=False forces the literal UTF-8 character instead of \uXXXX escapes
    print(json.dumps(payload, ensure_ascii=False))


# ──────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]

    if not args:
        gui_main()
        return

    match args[0]:
        case "--daemon":
            target = args[1] if len(args) > 1 else DEFAULT_APP
            asyncio.run(daemon_main(target))

        case "--status":
            cli_status()

        case "--stop":
            cli_stop()

        case "--waybar":
            cli_waybar()

        case "--help" | "-h":
            print(__doc__)

        case other:
            # Legacy compatibility: bare app name = --daemon <app>
            asyncio.run(daemon_main(other))


if __name__ == "__main__":
    main()
