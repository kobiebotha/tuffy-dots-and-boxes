#!/usr/bin/env python3
"""Generate a same-session live-download benchmark; credentials stay on badge."""
import argparse
from pathlib import Path
from performance_replay import build_source

HERE = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--variant', choices=('original', 'native'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--restore-native-cache', action='store_true')
    args = parser.parse_args()
    source = build_source(args.variant, storage='flash')
    source = source.split('database_module.ShadowVFS = MeterVFS')[0]
    assert not args.restore_native_cache or args.variant == 'native'
    args.output.write_text(source + '\nBENCH_RESTORE_NATIVE_CACHE=' + repr(args.restore_native_cache)
                           + '\n' + (HERE / 'performance_live_payload.py').read_text())


if __name__ == '__main__':
    main()
