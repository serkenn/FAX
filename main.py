#!/usr/bin/env python3
"""WEFAX RTL-SDR Decoder — entry point."""

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        description="WEFAX HF Weather FAX decoder via RTL-SDR"
    )
    parser.add_argument(
        "--freq", type=float, default=None,
        metavar="KHZ",
        help="Starting centre frequency in kHz (e.g. 8140)",
    )
    parser.add_argument(
        "--device", type=int, default=0,
        metavar="N",
        help="RTL-SDR device index (default: 0)",
    )
    parser.add_argument(
        "--gain", default="auto",
        metavar="DB",
        help="Tuner gain in dB or 'auto' (default: auto)",
    )
    parser.add_argument(
        "--no-direct-sampling", action="store_true",
        help="Disable Q-branch direct sampling (use for VHF/UHF frequencies)",
    )
    args = parser.parse_args()

    try:
        from gui import WEFAXApp
    except ImportError as exc:
        print(f"Import error: {exc}", file=sys.stderr)
        print("Install dependencies:  pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)

    app = WEFAXApp()

    # Pre-fill CLI arguments into the GUI
    if args.freq is not None:
        app._freq_var.set(str(args.freq))
    app._dev_var.set(args.device)
    app._gain_var.set(args.gain)
    if args.no_direct_sampling:
        app._direct_var.set(False)

    app.mainloop()


if __name__ == "__main__":
    main()
