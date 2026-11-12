#!/usr/bin/env python3
"""Copy limitwatch.py into each plugin's hooks/ with a hash header, or `--check` that the copies are current."""

import argparse
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGINS = [HERE.parent / 'claude-portavoce', HERE.parent / 'claude-goon']
HEADER = '# Vendored from claude-meridiana: edit it there and run sync.py.\n# body sha256: {}\n'


def vendored() -> str:
    body = (HERE / 'limitwatch.py').read_text()
    return HEADER.format(hashlib.sha256(body.encode()).hexdigest()) + body


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='report copies that differ and exit 1; write nothing')
    check = parser.parse_args().check
    expected, stale = vendored(), []
    for plugin in PLUGINS:
        target = plugin / 'hooks' / 'limitwatch.py'
        if not target.exists() or target.read_text() != expected:
            stale.append(target)
            if not check:
                target.write_text(expected)
    for target in stale:
        print(f'{"stale" if check else "updated"}: {target}')
    return 1 if check and stale else 0


if __name__ == '__main__':
    sys.exit(main())
