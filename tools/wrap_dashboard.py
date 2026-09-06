#!/usr/bin/env python3
"""Wrap a generated LearnedLeague dashboard in this site's chrome.

friends_dashboard.py (in the private data repo) produces a standalone page
that knows nothing about this site.  This adds the nav bar and the noindex
tag, so the presentation lives with the site rather than being duplicated
into the generator.

It is idempotent: re-wrapping an already-wrapped page is a no-op, so a
workflow can run it without tracking whether it ran before.

Usage:
    python tools/wrap_dashboard.py built.html LL/index.html
"""
import argparse
import os
import sys

MARKER = 'data-site-chrome'

HEAD = (
    '<link rel="stylesheet" href="/assets/site.css">'
    '<meta name="robots" content="noindex, nofollow">'
    '<meta name="description" content="LearnedLeague standings, careers and category stats.">'
    # The generated page puts its own padding on <body>; cancel the top and
    # pull the bar out to the edges so it reads as chrome, not content.
    '<style ' + MARKER + '>body{padding-top:0}.sitebar{margin:0 -18px 22px}</style>'
)

NAV = (
    '<nav class="sitebar" ' + MARKER + '><div class="sitebar-inner">'
    '<a class="home" href="/">istodayubadahslastday.art</a>'
    '<span aria-current="page">LearnedLeague</span>'
    '</div></nav>'
)

ANCHOR = '</head><body>'


def wrap(page):
    if MARKER in page:
        return page, False
    if page.count(ANCHOR) != 1:
        raise SystemExit(
            'Could not find a single "%s" to splice into -- the generator\'s '
            'markup changed; update tools/wrap_dashboard.py.' % ANCHOR)
    return page.replace(ANCHOR, HEAD + ANCHOR + NAV), True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', help='generated dashboard html')
    parser.add_argument('dest', help='where to write the wrapped page')
    args = parser.parse_args()

    with open(args.source, encoding='utf-8') as handle:
        page, changed = wrap(handle.read())

    os.makedirs(os.path.dirname(os.path.abspath(args.dest)), exist_ok=True)
    with open(args.dest, 'w', encoding='utf-8', newline='') as handle:
        handle.write(page)

    print('Wrote %s (%s, %d bytes)'
          % (args.dest, 'chrome added' if changed else 'already wrapped', len(page)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
