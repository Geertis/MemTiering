#!/usr/bin/env python3
"""Select a cold-medium implementation without changing Runtime messages."""
import argparse
import sys


def main():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--cold-media', choices=['cxl', 'ssd'], default='cxl')
    args, rest = parser.parse_known_args()
    sys.argv = [sys.argv[0], *rest]
    if args.cold_media == 'cxl':
        from cxl_controller import main as run
    else:
        from ssd_controller import main as run
    run()


if __name__ == '__main__':
    main()
