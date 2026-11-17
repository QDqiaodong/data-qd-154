"""signal-waveform-device: standalone offline device simulator command.

Runs the same real HTTP handler used by the test suite on a loopback port.
It is fully deterministic and needs no network access beyond localhost, no
accounts and no external services.
"""
import argparse
import signal
import time

from .device import DeviceServer, build_capture, default_raw_fn


def build_parser():
    parser = argparse.ArgumentParser(
        prog="signal-waveform-device",
        description="Offline HTTP measurement-device simulator (real TCP).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0,
                        help="TCP port; 0 picks a free port")
    parser.add_argument("--points", type=int, default=64)
    parser.add_argument("--sample-rate", type=int, default=1000)
    parser.add_argument("--dtype", choices=("int16", "uint16", "float32"),
                        default="int16")
    parser.add_argument("--byte-order", choices=("little", "big"),
                        default="little")
    parser.add_argument("--channels", default="a:0.001:0,b:0.002:1.5",
                        help="comma separated name:scale:offset channels")
    parser.add_argument("--capture-id", default="cap-0001")
    parser.add_argument("--max-range-bytes", type=int, default=4096)
    parser.add_argument("--no-lease", action="store_true",
                        help="simulate an ETag-only device")
    parser.add_argument("--lease-ttl", type=float, default=30.0)
    return parser


def _parse_channels(text):
    channels = []
    names = set()
    for item in text.split(","):
        parts = item.split(":")
        if len(parts) != 3:
            raise ValueError("channel spec must be name:scale:offset")
        name, scale, offset = parts
        if not name or name in names:
            raise ValueError("channel names must be unique and non-empty")
        names.add(name)
        channels.append((name, float(scale), float(offset)))
    if not 1 <= len(channels) <= 16:
        raise ValueError("need 1..16 channels")
    return channels


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    channels = _parse_channels(args.channels)
    capture = build_capture(
        args.capture_id, args.sample_rate, args.points, args.dtype,
        args.byte_order, channels, default_raw_fn)
    server = DeviceServer(
        capture, max_range_bytes=args.max_range_bytes,
        supports_lease=not args.no_lease, lease_ttl=args.lease_ttl,
        host=args.host, port=args.port)
    url = server.start()
    print("serving %s at %s (lease=%s, max_range_bytes=%d)"
          % (args.capture_id, url, not args.no_lease,
             args.max_range_bytes), flush=True)

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
