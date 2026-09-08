#!/usr/bin/env python3
"""Turn a folder of saved Music League round pages into ML/data/seasons.json.

Music League's page has no season name in it anywhere -- "Round 3" repeats
every season -- so the folder structure is what says which season a page
belongs to. Organise the drop folder as one subfolder per season, named
exactly as it appears in the dashboard already:

    private-data/MusicLeague/
      Season 1/
        round-01.html
        round-02.html
      Season 3/
        round-02.html

Each file is a full saved copy of a round's results page (right-click, Save
Page As, or the browser's "View Page Source" saved to a .html file -- the
same kind of file the /ML/?admin importer already accepts pasted, just from
disk instead of the clipboard, and in bulk). A season name that does not
already exist in ML/data/seasons.json is created; a round number that
already exists in that season is replaced, exactly as the browser importer
does.

This reimplements parseRoundHTML() from ML/template.html against a small
HTML tree built with the standard library, so it needs no browser and no
extra dependency; the two are meant to stay in step; if the page's markup
ever changes, both need updating together.

Usage:
    python tools/extract_ml.py --data private-data/MusicLeague
    python tools/extract_ml.py --data ... --check
"""
import argparse
import glob
import html.parser
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SEASONS_FILE = os.path.join(ROOT, 'ML', 'data', 'seasons.json')

VOID_TAGS = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
             'link', 'meta', 'param', 'source', 'track', 'wbr'}


# ============================================================================
# A minimal DOM: just enough to run the same queries parseRoundHTML() does.
# ============================================================================
class Node(object):
    __slots__ = ('tag', 'attrs', 'children', 'parent', 'text')

    def __init__(self, tag, attrs=None, text=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = None
        self.text = text          # only set on text nodes (tag is None)

    def classes(self):
        return set((self.attrs.get('class') or '').split())


class TreeBuilder(html.parser.HTMLParser):
    def __init__(self):
        html.parser.HTMLParser.__init__(self, convert_charrefs=True)
        self.root = Node('#root')
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, dict(attrs))
        node.parent = self.stack[-1]
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        # Recovers from an unclosed element by popping up to the matching
        # ancestor rather than only ever trusting the top of the stack --
        # real saved pages are not always perfectly balanced.
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if data:
            node = Node(None, text=data)
            node.parent = self.stack[-1]
            self.stack[-1].children.append(node)


def parse_tree(html_string):
    builder = TreeBuilder()
    builder.feed(html_string)
    return builder.root


def clean(s):
    return re.sub(r'\s+', ' ', s or '').strip()


def get_text(node):
    if node is None:
        return ''
    out = []

    def walk(n):
        if n.tag is None:
            out.append(n.text)
        else:
            for c in n.children:
                walk(c)
    walk(node)
    return clean(''.join(out))


def find_all(node, tag=None, cls=None, id_prefix=None):
    """
    Every descendant matching, in document order. cls, when given, must all
    be present -- 'col text-truncate' matches an element carrying both
    classes, in either order, the same as a compound CSS class selector.
    """
    want = set(cls.split()) if cls else None
    out = []

    def walk(n):
        for c in n.children:
            if c.tag is None:
                continue
            ok = (tag is None or c.tag == tag) and \
                 (want is None or want <= c.classes()) and \
                 (id_prefix is None or (c.attrs.get('id') or '').startswith(id_prefix))
            if ok:
                out.append(c)
            walk(c)
    walk(node)
    return out


def find_first(node, tag=None, cls=None, id_prefix=None):
    hits = find_all(node, tag=tag, cls=cls, id_prefix=id_prefix)
    return hits[0] if hits else None


def direct_children(node, tag=None, cls=None):
    want = set(cls.split()) if cls else None
    return [c for c in node.children if c.tag is not None
            and (tag is None or c.tag == tag)
            and (want is None or want <= c.classes())]


# ============================================================================
# The port of parseRoundHTML().
# ============================================================================
def parse_round_html(html_string):
    doc = parse_tree(html_string)

    name_el = find_first(doc, tag='h5', cls='card-title')
    round_name = get_text(name_el)

    m = re.search(r'ROUND (\d+)', html_string)
    round_num = int(m.group(1)) if m else None

    submissions, votes = [], []
    for card in find_all(doc, id_prefix='spotify:track:'):
        uri = card.attrs.get('id')

        title_holder = find_first(card, cls='card-title')
        title_a = find_first(title_holder, tag='a') if title_holder else None
        title = get_text(title_a)

        artist = ''
        body = direct_children(card, cls='card-body')
        if body:
            rows = direct_children(body[0], cls='row')
            if rows:
                cols = direct_children(rows[0], cls='col text-truncate')
                if cols:
                    ps = direct_children(cols[0], tag='p', cls='card-text')
                    if ps:
                        artist = get_text(ps[0])

        points_holder = find_first(card, cls='col-auto text-end')
        points_el = find_first(points_holder, tag='h3') if points_holder else None
        try:
            total_points = int(clean(get_text(points_el)) or 0)
        except ValueError:
            total_points = 0

        place, submitter_name = '', ''
        rank_card = find_first(card, cls='card mt-3')
        if rank_card is not None:
            rank_holder = find_first(rank_card, cls='col-auto text-center')
            rank_h6 = find_first(rank_holder, tag='h6') if rank_holder else None
            place = get_text(rank_h6)
            sub_holder = find_first(rank_card, cls='col text-truncate')
            sub_h6 = find_first(sub_holder, tag='h6') if sub_holder else None
            submitter_name = get_text(sub_h6)

        submissions.append({'uri': uri, 'title': title, 'artist': artist,
                            'submitterName': submitter_name,
                            'totalPoints': total_points, 'place': place})

        votes_container = find_first(card, id_prefix='votes-')
        if votes_container is not None:
            for row in direct_children(votes_container, cls='row'):
                voter_el = find_first(row, tag='b')
                voter_name = get_text(voter_el)
                comment_el = find_first(row, tag='span', cls='text-break')
                comment = get_text(comment_el)
                points = 0
                for col_auto in find_all(row, cls='col-auto'):
                    h6 = find_first(col_auto, tag='h6')
                    if h6 is not None:
                        try:
                            points = int(clean(get_text(h6)) or 0)
                        except ValueError:
                            points = 0
                        break
                if voter_name:
                    votes.append({'uri': uri, 'voterName': voter_name,
                                 'points': points, 'comment': comment})

    return {'roundName': round_name, 'roundNum': round_num,
            'submissions': submissions, 'votes': votes}


# ============================================================================
# Folder walk + merge, mirroring handleParse()'s "replace by round number,
# else append" rule.
# ============================================================================
def load_seasons():
    if os.path.exists(SEASONS_FILE):
        with io.open(SEASONS_FILE, encoding='utf-8') as fh:
            return json.load(fh)
    return {'seasons': {}}


def build(data_dir, check):
    if not os.path.isdir(data_dir):
        return [], []

    state = load_seasons()
    added, skipped = [], []

    season_dirs = sorted(d for d in glob.glob(os.path.join(data_dir, '*'))
                         if os.path.isdir(d))
    loose = [p for p in glob.glob(os.path.join(data_dir, '*.html'))]
    for path in loose:
        skipped.append('%s -- not in a season subfolder, skipped (the page '
                       'carries no season name, so the folder is the only '
                       'place one can come from)' % os.path.basename(path))

    for season_dir in season_dirs:
        season_name = os.path.basename(season_dir)
        files = sorted(glob.glob(os.path.join(season_dir, '*.html')) +
                      glob.glob(os.path.join(season_dir, '*.htm')))
        if not files:
            continue

        state['seasons'].setdefault(season_name, {'rounds': []})
        rounds = state['seasons'][season_name]['rounds']

        for path in files:
            with io.open(path, encoding='utf-8', errors='replace') as fh:
                raw = fh.read()
            try:
                parsed = parse_round_html(raw)
            except Exception as exc:                    # noqa: BLE001
                skipped.append('%s -- could not parse (%s)'
                               % (os.path.basename(path), exc))
                continue
            if not parsed['submissions']:
                skipped.append('%s -- no songs found, is this a round '
                               'results page?' % os.path.basename(path))
                continue
            if not parsed['roundNum']:
                skipped.append('%s -- could not detect a round number'
                               % os.path.basename(path))
                continue

            entry = {'roundNum': parsed['roundNum'],
                     'roundName': parsed['roundName'],
                     'submissions': parsed['submissions'],
                     'votes': parsed['votes']}
            existing = next((i for i, r in enumerate(rounds)
                             if r['roundNum'] == entry['roundNum']), None)
            verb = 'replaced' if existing is not None else 'added'
            if existing is not None:
                rounds[existing] = entry
            else:
                rounds.append(entry)
            added.append('%s: Round %d "%s" (%s) -- %d songs, %d votes'
                        % (season_name, entry['roundNum'], entry['roundName'],
                           verb, len(entry['submissions']), len(entry['votes'])))

        rounds.sort(key=lambda r: r['roundNum'])

    if added and not check:
        with io.open(SEASONS_FILE, 'w', encoding='utf-8', newline='') as fh:
            fh.write(json.dumps(state, indent=1, ensure_ascii=False) + '\n')

    return added, skipped


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data', required=True, metavar='DIR',
                        help='folder of season subfolders holding saved round pages')
    parser.add_argument('--check', action='store_true',
                        help='report what would change, write nothing')
    args = parser.parse_args()

    added, skipped = build(args.data, args.check)

    if not added and not skipped:
        print('No round pages found under %s' % args.data)
        return 0

    for line in added:
        print(('would add ' if args.check else 'added ') + line)
    for line in skipped:
        print('skipped ' + line)

    if added:
        if args.check:
            print('(--check: seasons.json not written)')
        else:
            print('Wrote %s' % SEASONS_FILE)
    return 0


if __name__ == '__main__':
    sys.exit(main())
