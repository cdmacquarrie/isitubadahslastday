#!/usr/bin/env python3
"""Build ML/index.html from ML/template.html plus the JSON in ML/data.

The published page is deliberately self-contained -- one file, no fetches, so
it works offline and from file:// -- but keeping the season data inline in the
committed html made every new round a 200KB diff.  The data therefore lives in
ML/data/*.json and gets inlined here at build time.

Adding a round: open /ML/?admin, paste the round in, download the updated
seasons.json, drop it into ML/data/, commit.  The workflow rebuilds the page.

Usage:
    python tools/build_ml.py            # write ML/index.html
    python tools/build_ml.py --check    # exit 1 if the page is out of date
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

TEMPLATE = os.path.join(ROOT, 'ML', 'template.html')
OUTPUT = os.path.join(ROOT, 'ML', 'index.html')
SEASONS = os.path.join(ROOT, 'ML', 'data', 'seasons.json')
DURATIONS = os.path.join(ROOT, 'ML', 'data', 'durations.json')

SEASONS_TOKEN = '__SEASONS_JSON__'
DURATIONS_TOKEN = '__DURATIONS_JSON__'


def read_json(path, expect_key=None):
    with open(path, encoding='utf-8') as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise SystemExit('%s: expected a JSON object' % path)
    if expect_key and expect_key not in data:
        raise SystemExit('%s: missing "%s" key' % (path, expect_key))
    return data


def build():
    with open(TEMPLATE, encoding='utf-8') as handle:
        template = handle.read()

    for token in (SEASONS_TOKEN, DURATIONS_TOKEN):
        if template.count(token) != 1:
            raise SystemExit('%s: expected exactly one %s' % (TEMPLATE, token))

    seasons = read_json(SEASONS, 'seasons')
    durations = read_json(DURATIONS)

    # Inlined as JS source, so the separators must stay compact and any "</"
    # has to be broken up -- an unescaped </script> inside a string literal
    # would close the script block early.
    def inline(obj):
        return json.dumps(obj, separators=(',', ':')).replace('</', '<\\/')

    page = template.replace(SEASONS_TOKEN, inline(seasons))
    page = page.replace(DURATIONS_TOKEN, inline(durations))

    rounds = sum(len(v.get('rounds', [])) for v in seasons['seasons'].values())
    summary = '%d seasons, %d rounds, %d song durations' % (
        len(seasons['seasons']), rounds, len(durations))
    return page, summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true',
                        help='verify the committed page matches the data; do not write')
    args = parser.parse_args()

    page, summary = build()

    if args.check:
        if not os.path.exists(OUTPUT):
            print('ML/index.html is missing')
            return 1
        with open(OUTPUT, encoding='utf-8') as handle:
            if handle.read() == page:
                print('ML/index.html is up to date (%s)' % summary)
                return 0
        print('ML/index.html is out of date -- run tools/build_ml.py')
        return 1

    with open(OUTPUT, 'w', encoding='utf-8', newline='') as handle:
        handle.write(page)
    print('Wrote %s (%s)' % (OUTPUT, summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
