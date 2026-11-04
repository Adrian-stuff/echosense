"""Command line entry point.

    echosense run        start the device software (+ web console)
    echosense bench      time the pipeline against the 300 ms budget
    echosense protocol   run the lab technical-performance protocol in simulation
    echosense info       show what this machine has (sensors, audio, TTS)
"""
from __future__ import annotations

import argparse
import asyncio
import platform
import signal
import socket
import sys
import webbrowser

from .config import list_profiles, load_config


def _lan_ips() -> list[str]:
    ips = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


def cmd_run(args):
    from .app import EchoSense
    from .server import ConsoleServer

    cfg = load_config(args.config, args.profile)
    if args.stride:
        cfg["processing"]["stride"] = args.stride
    app = EchoSense(cfg, source=args.source, scene=args.scene, audio=args.audio, record=args.record,
                    settings_path=args.settings)
    app.start()
    host = args.host or cfg["web"]["host"]
    port = args.port or cfg["web"]["port"]

    async def main():
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover - windows
                pass
        runner = None
        if cfg["web"]["enabled"] and not args.no_web:
            server = ConsoleServer(app, host, port)
            runner = await server.run()
            url = f"http://{'localhost' if host in ('0.0.0.0', '127.0.0.1') else host}:{port}/"
            print(f"\n  Echo-Sense console:  {url}")
            if host == "0.0.0.0":
                for ip in _lan_ips():
                    print(f"  on the network:      http://{ip}:{port}/")
            print("  Ctrl+C to stop\n")
            if not args.no_browser:
                loop.call_later(0.5, webbrowser.open, url)
        await stop.wait()
        if runner:
            await runner.cleanup()

    try:
        asyncio.run(main())
    finally:
        app.stop()


def cmd_info(args):
    cfg = load_config(args.config, args.profile)
    from .audio.speech import Speech
    from .sensors import available_sources

    print(f"Python      {sys.version.split()[0]} on {platform.system()} {platform.machine()} ({platform.node()})")
    print(f"Profile     {cfg['device_profile']}   (available: {', '.join(list_profiles())})")
    for s in available_sources(cfg):
        print(f"Source      {s['id']:<8} {'yes' if s['available'] else 'no '} {s.get('note', '')}")
    try:
        import sounddevice as sd

        dev = sd.query_devices(kind="output")
        print(f"Audio out   {dev['name']} ({int(dev['default_samplerate'])} Hz)")
    except Exception as e:
        print(f"Audio out   none ({e.__class__.__name__}: {e})")
    print(f"TTS         {Speech.detect_backend()}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="echosense", description="Echo-Sense assistive depth-to-3D-audio device software")
    ap.add_argument("--config", help="JSON file overriding the configuration")
    ap.add_argument("--profile", help=f"device profile ({', '.join(list_profiles())})")
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="run the device software and the web console")
    r.add_argument("--source", default="auto", help="auto | sim | kinect2 | kinect2-fake | kinect1 | replay:FILE.esd")
    r.add_argument("--scene", default="maysan_street", help="simulator scene id or path to a scene .json")
    r.add_argument("--audio", choices=["local", "none"], help="audio driver (default from profile)")
    r.add_argument("--host")
    r.add_argument("--port", type=int)
    r.add_argument("--settings", help="settings file (default ~/.echosense/settings.json)")
    r.add_argument("--record", help="record the depth stream to FILE.esd")
    r.add_argument("--stride", type=int, help="override processing stride")
    r.add_argument("--no-web", action="store_true")
    r.add_argument("--no-browser", action="store_true")
    r.set_defaults(fn=cmd_run)

    b = sub.add_parser("bench", help="time every stage against the latency budget")
    b.add_argument("--scene", default="maysan_street")
    b.add_argument("--frames", type=int, default=300)
    b.set_defaults(fn=lambda a: __import__("echosense.tools.bench", fromlist=["run"]).run(a))

    p = sub.add_parser("protocol", help="lab technical performance protocol, in simulation")
    p.add_argument("--quick", action="store_true", help="concrete + indoor only, 5 repetitions")
    p.add_argument("--reps", type=int, default=None)
    p.add_argument("--out", default="reports")
    p.set_defaults(fn=lambda a: __import__("echosense.tools.protocol", fromlist=["run"]).run(a))

    i = sub.add_parser("info", help="show sensors / audio / TTS available here")
    i.set_defaults(fn=cmd_info)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        args = ap.parse_args(["run", *(argv or sys.argv[1:])])
    args.fn(args)


if __name__ == "__main__":
    main()
