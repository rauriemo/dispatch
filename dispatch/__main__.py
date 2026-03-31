"""Entry point for `python -m dispatch`."""

import argparse

from dispatch.main import main


def cli():
    parser = argparse.ArgumentParser(description="Dispatch Voice Agent Hub")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Run in debug mode (keyboard input instead of mic/wake word)",
    )
    args = parser.parse_args()
    main(debug=args.debug)


cli()
