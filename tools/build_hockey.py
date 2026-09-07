#!/usr/bin/env python3
"""Build HK/index.html from HK/template.html plus HK/data/hockey.json.

Same shape as tools/build_ml.py: the published page is one self-contained
file, but its data lives beside it so a new week is a small diff rather than
a rewritten page.

The json is produced by tools/extract_hockey.py from the private Yahoo
exports; it holds team names only, never managers.

Usage:
    python tools/build_hockey.py
    python tools/build_hockey.py --check    # exit 1 if the page is out of date
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TEMPLATE = os.path.join(ROOT, 'HK', 'template.html')
OUTPUT = os.path.join(ROOT, 'HK', 'index.html')
DATA = os.path.join(ROOT, 'HK', 'data', 'hockey.json')
TOKEN = '__HOCKEY_JSON__'


def build():
    with open(TEMPLATE, encoding='utf-8') as handle:
        template = handle.read()
    if template.count(TOKEN) != 1:
        raise SystemExit('%s: expected exactly one %s' % (TEMPLATE, TOKEN))

    with open(DATA, encoding='utf-8') as handle:
        data = json.load(handle)
    for key in ('current', 'rosters'):
        if key not in data:
            raise SystemExit('%s: missing "%s"' % (DATA, key))

    # Guard the anonymising at build time too, not just at extract time: this
    # is the step that actually publishes, and the check is cheap.
    blob = json.dumps(data)
    for bad in ('"manager"', '"manager_name"', '"manager_email"', '"email"'):
        if bad in blob:
            raise SystemExit('%s: contains %s -- refusing to publish' % (DATA, bad))

    # Inlined as JS source, so "</" has to be broken up: an unescaped
    # </script> inside a string literal would close the block early.
    inline = json.dumps(data, separators=(',', ':')).replace('</', '<\\/')
    page = template.replace(TOKEN, inline)

    seasons = [data['current']] + data.get('history', [])
    summary = '%d seasons, %d teams, %d matches' % (
        len(seasons), len(data['current']['teams']),
        sum(len(s['matchups']) for s in seasons))
    return page, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true',
                        help='verify the committed page matches the data')
    args = parser.parse_args()

    page, summary = build()

    if args.check:
        if not os.path.exists(OUTPUT):
            print('HK/index.html is missing')
            return 1
        with open(OUTPUT, encoding='utf-8') as handle:
            if handle.read() == page:
                print('HK/index.html is up to date (%s)' % summary)
                return 0
        print('HK/index.html is out of date -- run tools/build_hockey.py')
        return 1

    with open(OUTPUT, 'w', encoding='utf-8', newline='') as handle:
        handle.write(page)
    print('Wrote %s (%s)' % (OUTPUT, summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
