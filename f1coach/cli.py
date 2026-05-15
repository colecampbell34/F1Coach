from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from typing import Sequence

from f1coach.dashboard import TelemetryRuntime, run_udp_listener, serve_dashboard
from f1coach.references import load_reference


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Live F1 UDP telemetry coach.")
    subparsers = parser.add_subparsers(dest="command")

    dashboard = subparsers.add_parser("dashboard", help="Run the live dashboard and UDP listener.")
    _add_common_args(dashboard)
    dashboard.add_argument("--http-host", default="127.0.0.1", help="Dashboard host. Default: 127.0.0.1")
    dashboard.add_argument("--http-port", type=int, default=8765, help="Dashboard port. Default: 8765")
    dashboard.add_argument(
        "--open-browser",
        action="store_true",
        help="Open the dashboard in the default browser after startup.",
    )

    listen = subparsers.add_parser("listen", help="Run the terminal-only UDP listener.")
    _add_common_args(listen)

    return parser


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bind", default="0.0.0.0", help="UDP IP address to bind. Default: 0.0.0.0")
    parser.add_argument("--port", type=int, default=20777, help="UDP port to listen on. Default: 20777")
    parser.add_argument(
        "--game",
        choices=("f1-24",),
        default="f1-24",
        help="Telemetry adapter to use. Default: f1-24",
    )
    parser.add_argument("--reference", help="Path to an imported reference lap JSON file.")
    parser.add_argument(
        "--show-packets",
        action="store_true",
        help="Print packet counters while listening. Useful for connection troubleshooting.",
    )


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0].startswith("-") and raw_args[0] not in {"-h", "--help"}:
        raw_args.insert(0, "dashboard")
    args = build_parser().parse_args(raw_args)
    if args.command is None:
        raw_args = ["dashboard"]
        args = build_parser().parse_args(raw_args)

    reference = load_reference(args.reference) if args.reference else None
    runtime = TelemetryRuntime(reference=reference)

    if args.command == "listen":
        print(f"F1Coach listening on udp://{args.bind}:{args.port}")
        print("Start driving in F1 24. Clean laps build a personal ideal-lap reference.")
        try:
            run_udp_listener(runtime, args.bind, args.port, show_packets=args.show_packets)
        except KeyboardInterrupt:
            print("\nStopped.")
        return 0

    stop_event = threading.Event()
    udp_thread = threading.Thread(
        target=run_udp_listener,
        args=(runtime, args.bind, args.port, args.show_packets, stop_event),
        daemon=True,
    )
    udp_thread.start()
    server = serve_dashboard(runtime, args.http_host, args.http_port)
    print(f"F1Coach UDP listener: udp://{args.bind}:{args.port}")
    dashboard_url = f"http://{args.http_host}:{args.http_port}"
    print(f"Dashboard: {dashboard_url}")
    print("Start driving in F1 24. Enter the game's theoretical best in the dashboard when available.")
    if args.open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(dashboard_url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stop_event.set()
        server.server_close()
    return 0
