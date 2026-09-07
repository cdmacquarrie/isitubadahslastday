#!/usr/bin/python
# Copyright (c) 2026 Cameron MacQuarrie, MIT License
# pylint: disable=R0902
# pylint: disable=R0913
# pylint: disable=R0914
# pylint: disable=R0912
# pylint: disable=R0915
# pylint: disable=C0209
# pylint: disable=C0103
"""
Arcadia Adjacent LLama Stats -- a self-contained Learned League dashboard.

Reads the league-wide season exports (the `*Leaguewide*.csv` files pulled
from lgwide.php, one row per player per season) and builds a single html
page covering a named set of players across their whole careers.

Because those exports hold every player in the league, each number is also
placed against everyone else at the same level in the same season.  That is
what lets a D rundle season and an A rundle season be compared honestly.

Optionally reads ll_qhist_export.json -- per-category tallies and a
pre-computed HUN similarity matrix built from profile question histories --
to add the category and similarity plates.

Nothing here touches the Learned League site.  The site sits behind a bot
check, so the csv exports and the question-history json are gathered by
hand and dropped into a data directory; this script only reads them.

Usage:
    python friends_dashboard.py
    python friends_dashboard.py --since 100 --open
    python friends_dashboard.py --title "Some Other Name" --sections core
    python friends_dashboard.py --find kohli
"""
import argparse
import bisect
import csv
import datetime
import glob
import html
import json
import math
import os
import statistics
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_DATA_ROOTS = [
    os.path.join(HERE, '..', 'll_data'),
    os.path.join(HERE, '..', 'll105_data'),
    os.path.join('~', 'LL103'),
]

DEFAULT_ROSTER = os.path.join(HERE, 'friends_roster.csv')
DEFAULT_OUTPUT = os.path.join(HERE, 'generated_html', 'friends_dashboard.html')
DEFAULT_TITLE = 'Arcadia Adjacent LLama Stats'

#
# Roster rows with no group land here.
#
DEFAULT_GROUP = 'Other'

#
# Written out if no roster csv exists.  Player names are Learned League user
# names throughout -- they are what everyone actually goes by.  player_id is
# only needed to line a player up with the question-history export.
#
SEED_ROSTER = [
    'MacQuarrieC2', 'MacQuarrieD', 'MacQuarrieE', 'MacQuarrieM', 'KohliP',
    'KohliN', 'KohliJ', 'KohliB', 'KohliColi', 'CabotC', 'SharmaSS', 'WoodH',
    'WoodJ8', 'WoodB3', 'Woodhen', 'GreenoughH', 'LaneRywin', 'BenioffD',
    'CelebiM', 'MulvaneyM', 'SabbaghU', 'ForthS', 'YorkR', 'PlottT',
    'FutiaR', 'De-KayneR', 'CarlsonK', 'BulowC', 'MorganA5', 'WaltonZ',
]

#
# Rundle levels, strongest first.  Older exports have no Level column, so it
# is taken from the first word of the rundle name ('D Skyline Div 2').
#
LEVELS = ['A', 'B', 'C', 'D', 'E', 'R']
LEVEL_COLOUR = {
    'A': '#6a4c93', 'B': '#1982c4', 'C': '#2a9d8f',
    'D': '#8ab17d', 'E': '#e9c46a', 'R': '#b8b8b8',
}

#
# Metrics carried from the export.  scale is applied for display only; the
# percentile is computed on the raw value, which is monotonic either way.
#
METRICS = [
    ('Pts', 'Pts', 0, 1.0),
    ('QPct', 'QPct', 1, 100.0),
    ('TCA', 'TCA', 0, 1.0),
    ('MPD', 'MPD', 0, 1.0),
    ('OE', 'OE', 3, 1.0),
    ('DE', 'DE', 3, 1.0),
]

#
# Percentiled exactly like METRICS, but read by the offence/defence and
# record sections rather than shown as columns in the ratings grid, which is
# already as wide as it wants to be.
#
# Relationships below were checked against the exports and hold on every row:
#   Pts  = 2*W + T - FL      a forfeit loss costs a point beyond the loss
#   MPD  = TMP - TPA
#   PCA  = TMP / TCA         match points earned per correct answer
#   PCAA = TPA / CAA         points conceded per opponent correct answer
#   NUfP = UfPE - UfPA
#
EXTRA_METRICS = [
    ('PCA', 'PCA', 2, 1.0),
    ('PCAA', 'PCAA', 2, 1.0),
    ('UfPE', 'UfPE', 0, 1.0),
    ('UfPA', 'UfPA', 0, 1.0),
    ('NUfP', 'NUfP', 0, 1.0),
    ('FL', 'FL', 0, 1.0),
]

ALL_METRICS = METRICS + EXTRA_METRICS

#
# Fields where a low number is the good outcome, so a raw "percentage of
# players at or below" reads backwards and has to be flipped before it is
# shown as a rating.
#
LOWER_IS_BETTER = {'PCAA', 'UfPA', 'FL'}

#
# Reading order: the season being played, then how it was played, then the
# careers behind it, then the question-level plates, then the cohort, with
# the lighter material and the method notes last.
#
ALL_SECTIONS = ['summary', 'standings', 'outlook', 'ratings', 'profile',
                'record',
                'scatter', 'moves', 'careers', 'levels', 'explorer',
                'categories',
                'ranks', 'hun', 'pca', 'groups', 'referrals', 'awards',
                'about']
SECTION_SETS = {
    'all': ALL_SECTIONS,
    'core': ['summary', 'standings', 'outlook', 'careers', 'groups',
             'awards', 'about'],
    'career': ['careers', 'levels', 'about'],
    'season': ['standings', 'outlook', 'ratings', 'profile', 'record',
               'scatter', 'moves', 'about'],
    'groups': ['groups', 'referrals', 'about'],
    'plates': ['categories', 'ranks', 'hun', 'pca', 'about'],
    'share': ['ranks'],
    'pca': ['pca', 'about'],
}

GLOSSARY = [
    ('Pts', 'Standings points: 3 for a win, 1 for a tie.'),
    ('W-L-T', 'Wins, losses and ties.'),
    ('TCA', 'Total correct answers.'),
    ('QPct', 'Share of questions answered correctly, shown as a percentage '
             '(the export stores it as a fraction).'),
    ('MPD', 'Match point differential: total match points scored minus '
            'those allowed.'),
    ('OE / DE', 'Offensive and defensive efficiency, carried through from '
                'the export unchanged.'),
    ('PCA', 'Match points won per correct answer (TMP divided by TCA). How '
            'much each right answer was actually worth, which depends on '
            'where the opponent placed their defence.'),
    ('PCAA', 'Points conceded per opponent correct answer (TPA divided by '
             'CAA). The same measure seen from the other side: low is good.'),
    ('UfPE / UfPA', 'Unforced points earned and allowed. Points that changed '
                    'hands because defence was set against the wrong '
                    'questions rather than because of who knew what.'),
    ('NUfP', 'Net unforced points: UfPE minus UfPA. Positive means a player '
             'gained more from opponents misjudging them than they gave '
             'away misjudging opponents.'),
    ('FL', 'Forfeit losses. Charged twice over: the match is lost, and a '
           'further standings point is deducted. Points are 2 per win and 1 '
           'per tie, less one for each forfeit loss, which the exports bear '
           'out on every row.'),
    ('Margin', 'Standings-points percentile minus match-point-differential '
               'percentile. Matches pay the same for a narrow win as a wide '
               'one, so the two can drift apart; positive means the record '
               'flatters the scoring behind it.'),
    ('r / R&sup2;', 'In the correlation explorer: Pearson correlation and '
                    'the share of variance the straight-line fit accounts '
                    'for. The p value is a normal approximation, meant to '
                    'separate a pattern from noise rather than to serve as '
                    'a formal test.'),
    ('Level pct', 'Percentile against every player at the same level (A-E, '
                  'R) in the same season. 50 is a typical player for that '
                  'level, 90 is the top tenth. Computed here.'),
    ('Conversion', 'Points percentile minus correct-answers percentile, both '
                   'within level. Positive means a player turns what they '
                   'know into more standings points than their peers do.'),
    ('Swing', 'Mean level percentile of the three most recent seasons minus '
              'the three before them. Recent direction of travel.'),
    ('Spread', 'Standard deviation of level percentile across a career. Low '
               'is metronomic, high is streaky.'),
    ('PC1 / PC2', 'The two directions along which this roster varies most, '
                  'from a principal component analysis of the standardised '
                  'inputs. The percentage on each axis is how much of the '
                  'total variation that direction accounts for.'),
    ('HUN', 'Hamill/Usui number: the share of questions two players both '
            'answered on which they got the same result. Higher is more '
            'alike. Pairs sharing fewer than 100 questions are greyed, '
            'being too thin to read much into.'),
]


def norm_name(name):
    """
    Names are spelled inconsistently between exports ('Celebi M' versus
    'CelebiM'), so compare on a squashed form.
    """
    return ''.join(name.split()).lower()


def to_float(value, default=None):
    """
    Parse a csv cell that may be blank or malformed.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def read_csv_rows(path):
    """
    Read a csv file into a list of dicts.

    The exports are not reliably utf-8, so decoding is lenient.  Newer ones
    also carry a byte order mark that would otherwise ride along on the
    first field name and stop 'Season' from ever matching; utf-8-sig eats it.
    """
    with open(path, newline='', encoding='utf-8-sig',
              errors='replace') as fdesc:
        return list(csv.DictReader(fdesc))


def find_files(roots, pattern):
    """
    Search each root, and one level of subdirectory, for a glob pattern.
    """
    found = []
    for root in roots:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        for probe in [os.path.join(root, pattern),
                      os.path.join(root, '*', pattern)]:
            for path in glob.glob(probe):
                if path not in found:
                    found.append(path)
    return sorted(found)


def level_of(row):
    """
    Level for a row, falling back to the first word of the rundle name for
    the older exports that have no Level column.
    """
    level = (row.get('Level') or '').strip()
    if level:
        return level
    rundle = (row.get('Rundle') or '').strip()
    return rundle.split(' ')[0] if rundle else ''


class SeasonData(object):
    """
    One season of the league, reduced on the way in.

    Two dozen seasons of full league exports will not fit comfortably in
    memory all at once, and none of it is needed: the percentiles only want
    a sorted list of values per level, and everything else only concerns the
    roster.  So the rows are streamed, the distributions accumulated, and
    only roster rows kept.
    """
    def __init__(self, path, wanted):
        self.path = path
        self.season = None
        self.matchday = None
        self.rows = {}
        self.total = 0
        self.level_counts = {}
        self.rundle_pts = {}
        cohorts = {}
        for row in read_csv_rows(path):
            if self.season is None:
                self.season = int(row['Season'])
                self.matchday = int(row['Matchday'])
            self.total += 1
            level = level_of(row)
            self.level_counts[level] = self.level_counts.get(level, 0) + 1
            for field, _, _, _ in ALL_METRICS:
                value = to_float(row.get(field))
                if value is not None:
                    cohorts.setdefault((level, field), []).append(value)
            #
            # Every player's points, kept per rundle, so a roster member's
            # position can be read against the people they actually play.
            #
            pts = to_float(row.get('Pts'))
            if pts is not None:
                self.rundle_pts.setdefault(row.get('Rundle', ''),
                                           []).append(pts)
            key = norm_name(row['Player'])
            if key in wanted:
                row['_level'] = level
                self.rows[key] = row
        if self.season is None:
            raise ValueError('no rows')
        for series in cohorts.values():
            series.sort()
        self.cohorts = cohorts
        for series in self.rundle_pts.values():
            series.sort(reverse=True)

    def percentile(self, level, field, value):
        """
        Percentage of same-level players at or below value.
        """
        series = self.cohorts.get((level, field))
        if not series or value is None:
            return None
        return 100.0 * bisect.bisect_right(series, value) / len(series)

    def cohort_size(self, level):
        """
        How many players sit at this level.
        """
        return self.level_counts.get(level, 0)


def load_seasons(roots, wanted, since=None, until=None):
    """
    Load every league-wide export found, keeping only the roster's rows.
    """
    seasons = {}
    for path in find_files(roots, '*Leaguewide*.csv'):
        try:
            data = SeasonData(path, wanted)
        except (ValueError, KeyError, IndexError) as err:
            print('  skipping %s (%s)' % (os.path.basename(path), err))
            continue
        if since is not None and data.season < since:
            continue
        if until is not None and data.season > until:
            continue
        keep = seasons.get(data.season)
        #
        # A season can turn up twice (an old partial export next to a newer
        # complete one).  The later match day wins.
        #
        if keep is None or data.matchday > keep.matchday:
            seasons[data.season] = data
    return seasons


def load_roster(path):
    """
    Read the roster csv, writing a seed copy first if it is missing.

    A 'group' column from an older version of this script is ignored, and
    a 'display' column is ignored too -- user names are used throughout.
    """
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        with open(path, 'w', newline='', encoding='utf-8') as fdesc:
            writer = csv.writer(fdesc)
            writer.writerow(['username', 'player_id'])
            for name in SEED_ROSTER:
                writer.writerow([name, ''])
        print('Wrote a starter roster to %s' % path)
    roster = []
    seen = set()
    for row in read_csv_rows(path):
        username = (row.get('username') or '').strip()
        if not username or username in seen:
            continue
        seen.add(username)
        roster.append({
            'username': username,
            'player_id': (row.get('player_id') or '').strip(),
            'group': (row.get('group') or '').strip() or DEFAULT_GROUP,
            'referrer': (row.get('referrer') or '').strip(),
        })
    return roster


def group_order(roster):
    """
    Groups in a sensible reading order: the named cohorts as they first
    appear in the csv, with the catch-all last.
    """
    order = []
    for person in roster:
        if person['group'] not in order:
            order.append(person['group'])
    return ([g for g in order if g != DEFAULT_GROUP] +
            [g for g in order if g == DEFAULT_GROUP])


def load_qhist_export(roots):
    """
    Load the question-history export: per-category tallies and the HUN
    pairs, both keyed by user name.
    """
    for path in find_files(roots, 'll_qhist_export.json'):
        try:
            with open(path, encoding='utf-8') as fdesc:
                data = json.load(fdesc)
        except (ValueError, OSError):
            continue
        if 'names' in data and 'pairs' in data:
            data['_path'] = path
            return data
    return None


def build_careers(roster, seasons):
    """
    One record per player holding every season they appear in, with the
    percentiles attached, plus the career aggregates derived from them.
    """
    careers = []
    for person in roster:
        key = norm_name(person['username'])
        entries = []
        for season in sorted(seasons):
            data = seasons[season]
            row = data.rows.get(key)
            if row is None:
                continue
            level = row['_level']
            entry = {
                'season': season,
                'matchday': data.matchday,
                'level': level,
                'level_ix': LEVELS.index(level) if level in LEVELS else None,
                'rundle': row.get('Rundle', ''),
                'rank': int(to_float(row.get('Rundle Rank'), 0)),
                'wins': to_float(row.get('Wins'), 0),
                'losses': to_float(row.get('Losses'), 0),
                'ties': to_float(row.get('Ties'), 0),
                'cohort': data.cohort_size(level),
                'values': {},
                'pcts': {},
            }
            entry['record'] = '%d-%d-%d' % (entry['wins'], entry['losses'],
                                            entry['ties'])
            for field, _, _, _ in ALL_METRICS:
                value = to_float(row.get(field))
                entry['values'][field] = value
                entry['pcts'][field] = data.percentile(level, field, value)
            entry['rundle_field'] = data.rundle_pts.get(entry['rundle'], [])
            pts_pct = entry['pcts'].get('Pts')
            tca_pct = entry['pcts'].get('TCA')
            mpd_pct = entry['pcts'].get('MPD')
            entry['conversion'] = (None if pts_pct is None or tca_pct is None
                                   else pts_pct - tca_pct)
            #
            # Conversion asks whether knowledge became points. Margin asks a
            # narrower question: given the match points a player actually
            # scored, did the win-loss column follow? Matches turn on the
            # margin, so a heavy win and a narrow loss pay the same.
            #
            entry['margin'] = (None if pts_pct is None or mpd_pct is None
                               else pts_pct - mpd_pct)
            entries.append(entry)
        if not entries:
            careers.append({'username': person['username'],
                            'player_id': person['player_id'],
                            'group': person['group'],
                            'referrer': person['referrer'],
                            'seasons': [], 'found': False})
            continue
        careers.append(summarise(person, entries))
    return careers


def summarise(person, entries):
    """
    Roll a player's seasons up into the career figures the tables want.
    """
    pts_pcts = [ent['pcts']['Pts'] for ent in entries
                if ent['pcts'].get('Pts') is not None]
    wins = sum(ent['wins'] for ent in entries)
    losses = sum(ent['losses'] for ent in entries)
    ties = sum(ent['ties'] for ent in entries)
    played = wins + losses + ties
    tca = sum(ent['values'].get('TCA') or 0 for ent in entries)
    #
    # Promotions and relegations only count between seasons actually played
    # back to back; a gap year says nothing about form.
    #
    promos = relegs = 0
    for before, after in zip(entries, entries[1:]):
        if (after['season'] - before['season'] != 1 or
                before['level_ix'] is None or after['level_ix'] is None):
            continue
        if after['level_ix'] < before['level_ix']:
            promos += 1
        elif after['level_ix'] > before['level_ix']:
            relegs += 1
    levels_seen = [ent['level_ix'] for ent in entries
                   if ent['level_ix'] is not None]
    best = max(entries, key=lambda ent: ent['pcts'].get('Pts') or -1)
    worst = min(entries, key=lambda ent: ent['pcts'].get('Pts')
                if ent['pcts'].get('Pts') is not None else 999)
    swing = None
    if len(pts_pcts) >= 4:
        tail = pts_pcts[-3:]
        head = pts_pcts[-6:-3] or pts_pcts[:-3]
        if head:
            swing = statistics.mean(tail) - statistics.mean(head)
    return {
        'username': person['username'],
        'player_id': person['player_id'],
        'group': person['group'],
        'referrer': person['referrer'],
        'found': True,
        'seasons': entries,
        'first': entries[0]['season'],
        'last': entries[-1]['season'],
        'count': len(entries),
        'wins': wins, 'losses': losses, 'ties': ties, 'played': played,
        'win_pct': (wins + 0.5 * ties) / played if played else 0.0,
        'tca': tca,
        'titles': sum(1 for ent in entries if ent['rank'] == 1),
        'promotions': promos,
        'relegations': relegs,
        'peak_level': LEVELS[min(levels_seen)] if levels_seen else '',
        'mean_pct': statistics.mean(pts_pcts) if pts_pcts else None,
        'spread': (statistics.pstdev(pts_pcts)
                   if len(pts_pcts) > 1 else None),
        'swing': swing,
        'best': best,
        'worst': worst,
    }


def current_entries(careers, season):
    """
    Every player's row for one season, best standings points first.
    """
    out = []
    for car in careers:
        for ent in car['seasons']:
            if ent['season'] == season:
                item = dict(ent)
                item['username'] = car['username']
                item['group'] = car['group']
                out.append(item)
    out.sort(key=lambda ent: (-(ent['values'].get('Pts') or -999),
                              ent['username'].lower()))
    return out


def season_moves(careers, prev_season, this_season):
    """
    Pair up the two most recent seasons for players who appear in both.
    """
    moves = []
    for car in careers:
        by_season = {ent['season']: ent for ent in car['seasons']}
        before, after = by_season.get(prev_season), by_season.get(this_season)
        if before is None or after is None:
            continue
        level_move = 0
        if before['level_ix'] is not None and after['level_ix'] is not None:
            level_move = before['level_ix'] - after['level_ix']
        moves.append({
            'username': car['username'],
            'before': before, 'after': after,
            'd_pts': ((after['values'].get('Pts') or 0) -
                      (before['values'].get('Pts') or 0)),
            'd_qpct': 100.0 * ((after['values'].get('QPct') or 0) -
                               (before['values'].get('QPct') or 0)),
            'level_move': level_move,
        })
    moves.sort(key=lambda mve: -mve['d_pts'])
    return moves


def superlatives(careers, current, moves):
    """
    The awards panel: some for the season in progress, some for careers.
    """
    awards = []

    def best_of(items, key, title, note, shape, factor=1.0, low=False):
        """
        Take the leader on one measure, skipping anyone missing it.
        """
        ranked = [it for it in items if key(it) is not None]
        if not ranked:
            return
        ranked.sort(key=key, reverse=not low)
        win = ranked[0]
        awards.append((title, win['username'],
                       shape % (key(win) * factor), note))

    best_of(current, lambda e: e['values'].get('Pts'),
            'Top of the table', 'Most standings points now', '%.0f pts')
    best_of(current, lambda e: e['pcts'].get('Pts'),
            'Best for their level', 'Highest points percentile',
            '%.0fth pct')
    best_of(current, lambda e: e['values'].get('QPct'),
            'Sharpest shooter', 'Highest question percentage',
            '%.1f%% correct', 100.0)
    best_of(current, lambda e: e['values'].get('DE'),
            'The wall', 'Best defensive efficiency', '%.3f DE')
    best_of(current, lambda e: e['conversion'],
            'Best conversion', 'Knowledge into standings points',
            '%+.0f pctile')
    best_of(current, lambda e: e['conversion'],
            'Hard luck', 'Knows more than the table shows',
            '%+.0f pctile', low=True)
    played = [car for car in careers if car.get('found')]
    best_of(played, lambda c: c['count'],
            'Longest server', 'Most seasons played', '%.0f seasons')
    best_of(played, lambda c: c['mean_pct'],
            'Best career', 'Highest mean level percentile', '%.0fth pct')
    best_of(played, lambda c: c['titles'] or None,
            'Rundle titles', 'Times finished first', '%.0f titles')
    best_of(played, lambda c: c['spread'],
            'Most metronomic', 'Smallest career spread', '%.0f spread',
            low=True)
    best_of(played, lambda c: c['swing'],
            'Rising fastest', 'Best recent swing', '%+.0f pctile')
    if moves:
        awards.append(('Most improved', moves[0]['username'],
                       '%+.0f pts' % moves[0]['d_pts'],
                       'Biggest gain on last season'))
    return awards


#
# ----------------------------------------------------------------------
# Rendering.  One self-contained page: no external stylesheet, font,
# script or image, so it can be mailed around and still work.
# ----------------------------------------------------------------------
#

CSS = """
:root {
  color-scheme: light dark;
  --bg:#f5f6f8; --panel:#fff; --ink:#15181d; --muted:#5e6673; --line:#e3e6eb;
  --accent:#2f6f4f; --accent-soft:#e7f0ea; --warm:#a4543a; --grid:#eceff3;
}
:root[data-theme="dark"], html.dark {
  --bg:#111317; --panel:#191c21; --ink:#e8eaed; --muted:#98a0ac;
  --line:#2a2f37; --accent:#6fbf8f; --accent-soft:#1d2a23; --warm:#d98f6f;
  --grid:#222730;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:#111317; --panel:#191c21; --ink:#e8eaed; --muted:#98a0ac;
    --line:#2a2f37; --accent:#6fbf8f; --accent-soft:#1d2a23;
    --warm:#d98f6f; --grid:#222730;
  }
}
*{box-sizing:border-box}
body{margin:0;padding:26px 18px 72px;background:var(--bg);color:var(--ink);
 font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,
 Arial,sans-serif}
.wrap{max-width:1180px;margin:0 auto}
header{margin-bottom:20px;display:flex;flex-wrap:wrap;align-items:flex-end;
 gap:14px;justify-content:space-between}
h1{font-size:30px;margin:0 0 5px;letter-spacing:-.01em}
.sub{color:var(--muted);font-size:14px;margin:0}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.tools input,.tools button,.tools select{font:inherit;font-size:13px;padding:6px 10px;
 border:1px solid var(--line);border-radius:7px;background:var(--panel);
 color:var(--ink)}
.tools button,.tools select{cursor:pointer}
.tools button:hover{border-color:var(--accent)}
nav.toc{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 18px}
nav.toc a{font-size:12px;text-decoration:none;color:var(--muted);
 border:1px solid var(--line);border-radius:999px;padding:3px 10px}
nav.toc a:hover{color:var(--accent);border-color:var(--accent)}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;
 padding:18px 20px;margin-bottom:18px}
h2{font-size:18px;margin:0 0 4px}
h2 .fold{float:right;font-size:12px;font-weight:400;color:var(--muted);
 cursor:pointer;user-select:none}
.note{color:var(--muted);font-size:13px;margin:0 0 14px}
table{width:100%;border-collapse:collapse;font-size:13.5px}
th,td{padding:6px 8px;text-align:right;border-bottom:1px solid var(--line)}
th:first-child,td:first-child{text-align:left}
th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;
 letter-spacing:.04em;white-space:nowrap;cursor:pointer}
tbody tr:hover{background:var(--accent-soft)}
td.name{font-weight:600;white-space:nowrap}
td.dim,.dim{color:var(--muted)}
.scroll{overflow-x:auto}
.scroll table{min-width:680px}
.pill{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;
 font-weight:700;color:#fff}
.bar{position:relative;display:inline-block;height:15px;width:100%;
 min-width:74px;background:var(--grid);border-radius:3px;vertical-align:middle}
.bar>i{position:absolute;left:0;top:0;bottom:0;background:var(--accent);
 border-radius:3px}
.bar>span{position:absolute;right:4px;top:-1px;font-size:10.5px}
.diverge{position:relative;display:inline-block;height:15px;width:100%;
 min-width:130px;background:var(--grid);border-radius:3px;
 overflow:hidden;vertical-align:middle}
.diverge>i{position:absolute;top:0;bottom:0;border-radius:2px;
 opacity:.85}
.lead{font-size:14.5px;line-height:1.6;color:var(--ink);margin:0 0 16px;
 max-width:74ch}
.ex-controls{display:flex;gap:14px;flex-wrap:wrap;align-items:center;
 margin:0 0 14px}
.ex-controls label{font-size:12px;color:var(--muted);display:flex;
 gap:6px;align-items:center}
.ex-controls select{font:inherit;font-size:12.5px;padding:5px 8px;
 border:1px solid var(--line);border-radius:7px;background:var(--panel);
 color:var(--ink)}
.ex-stats{margin-top:10px;font-size:13px;color:var(--ink)}
.ex-stats .sep{color:var(--muted);margin:0 9px}
.ex-stats b{font-weight:600;color:var(--muted);font-size:11px;
 text-transform:uppercase;letter-spacing:.04em;margin-right:4px}
.diverge>.mid{position:absolute;left:50%;top:0;bottom:0;width:1px;
 background:var(--muted);opacity:.45}
.up{color:var(--accent);font-weight:600}
.down{color:var(--warm);font-weight:600}
.cards{display:grid;gap:10px;
 grid-template-columns:repeat(auto-fill,minmax(196px,1fr))}
.card{border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card .label{font-size:10.5px;text-transform:uppercase;letter-spacing:.05em;
 color:var(--muted)}
.card .who{font-size:17px;font-weight:650;margin:2px 0 1px;word-break:break-all}
.card .val{font-size:12.5px;color:var(--accent);font-weight:600}
.card .why{font-size:11.5px;color:var(--muted);margin-top:3px}
svg{max-width:100%;height:auto;display:block}
dl.gloss{margin:0;font-size:13px}
dl.gloss dt{font-weight:650;margin-top:8px}
dl.gloss dd{margin:2px 0 0;color:var(--muted)}
footer{color:var(--muted);font-size:12px;margin-top:10px}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
 font-size:12px}
tr.hidden{display:none}
"""

PAGE_JS = """
document.querySelectorAll('table.sortable').forEach(function (table) {
  table.querySelectorAll('th').forEach(function (head, col) {
    head.addEventListener('click', function () {
      var body = table.tBodies[0];
      var rows = Array.prototype.slice.call(body.rows);
      var dir = head.dataset.dir === 'asc' ? -1 : 1;
      table.querySelectorAll('th').forEach(function (o) { delete o.dataset.dir; });
      head.dataset.dir = dir === 1 ? 'asc' : 'desc';
      rows.sort(function (l, r) {
        var a = l.cells[col] ? l.cells[col].dataset.sort : undefined;
        var b = r.cells[col] ? r.cells[col].dataset.sort : undefined;
        if (a === undefined) { a = l.cells[col] ? l.cells[col].textContent : ''; }
        if (b === undefined) { b = r.cells[col] ? r.cells[col].textContent : ''; }
        var x = parseFloat(a), y = parseFloat(b);
        if (!isNaN(x) && !isNaN(y)) { return (x - y) * dir; }
        return String(a).localeCompare(String(b)) * dir;
      });
      rows.forEach(function (row) { body.appendChild(row); });
    });
  });
});
var box = document.getElementById('filter');
var picker = document.getElementById('group');
/* Group comes from the roster rather than the markup: only a couple of the
   tables carry a group column, but every player row starts with the user
   name, so the lookup covers all of them from one place.  Rows whose first
   cell is not a player -- the by-group rollup, the category plates -- are
   left alone by the group filter, since hiding them would empty a table the
   filter says nothing about. */
var GROUPS = window.LL_GROUPS || {};
function applyFilters() {
  var q = box ? box.value.trim().toLowerCase() : '';
  var want = picker ? picker.value : '';
  document.querySelectorAll('table tbody tr').forEach(function (row) {
    var cell = row.cells[0];
    var text = cell ? cell.textContent.trim() : '';
    var hit = !q || text.toLowerCase().indexOf(q) >= 0;
    if (hit && want && Object.prototype.hasOwnProperty.call(GROUPS, text)) {
      hit = GROUPS[text] === want;
    }
    row.classList.toggle('hidden', !hit);
  });
  if (typeof EXPLORER !== 'undefined') { EXPLORER.draw(); }
}
if (box) { box.addEventListener('input', applyFilters); }
if (picker) { picker.addEventListener('change', applyFilters); }
document.querySelectorAll('h2 .fold').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var sec = btn.closest('section');
    var shut = sec.dataset.shut === '1';
    sec.dataset.shut = shut ? '0' : '1';
    btn.textContent = shut ? 'hide' : 'show';
    Array.prototype.slice.call(sec.children).forEach(function (kid) {
      if (kid.tagName !== 'H2') { kid.style.display = shut ? '' : 'none'; }
    });
  });
});
/* Correlation explorer. LL_ROWS is one entry per player-season, as
   [username, group, season, level, values[], percentiles[]] against the
   field order in LL_FIELDS. It shares the page's filter box and group
   picker, so whatever is on screen is what gets fitted. */
var EXPLORER = (function () {
  var xSel = document.getElementById('ex-x');
  var ySel = document.getElementById('ex-y');
  var scaleSel = document.getElementById('ex-scale');
  var lineBox = document.getElementById('ex-line');
  var plot = document.getElementById('ex-plot');
  var stats = document.getElementById('ex-stats');
  if (!xSel || !plot) { return { draw: function () {} }; }

  var W = 720, H = 430, PL = 62, PR = 20, PT = 18, PB = 48;
  var IW = W - PL - PR, IH = H - PT - PB;

  function rowsInView() {
    var want = picker ? picker.value : '';
    var q = box ? box.value.trim().toLowerCase() : '';
    return LL_ROWS.filter(function (r) {
      if (want && r[1] !== want) { return false; }
      if (q && r[0].toLowerCase().indexOf(q) < 0) { return false; }
      return true;
    });
  }

  /* Pearson r, plus the ordinary least squares line. Guards the degenerate
     cases -- fewer than three points, or no spread on an axis -- because a
     line through them is not a finding, it is an artefact. */
  function fit(pts) {
    var n = pts.length;
    if (n < 3) { return null; }
    var sx = 0, sy = 0, i;
    for (i = 0; i < n; i++) { sx += pts[i][0]; sy += pts[i][1]; }
    var mx = sx / n, my = sy / n;
    var vxx = 0, vyy = 0, vxy = 0, dx, dy;
    for (i = 0; i < n; i++) {
      dx = pts[i][0] - mx; dy = pts[i][1] - my;
      vxx += dx * dx; vyy += dy * dy; vxy += dx * dy;
    }
    if (vxx <= 0 || vyy <= 0) { return null; }
    var r = vxy / Math.sqrt(vxx * vyy);
    var slope = vxy / vxx;
    return { n: n, r: r, slope: slope, intercept: my - slope * mx };
  }

  /* Rough two-sided significance for r, via a normal approximation to
     Fisher's z. Enough to separate "worth a look" from "noise"; it is not
     a substitute for a real test, and the caption says so. */
  function pValue(r, n) {
    if (n < 5 || Math.abs(r) >= 1) { return null; }
    var z = 0.5 * Math.log((1 + r) / (1 - r)) * Math.sqrt(n - 3);
    var a = Math.abs(z);
    var t = 1 / (1 + 0.2316419 * a);
    var d = 0.3989423 * Math.exp(-a * a / 2);
    var p = d * t * (0.3193815 + t * (-0.3565638 + t * (1.781478 +
            t * (-1.821256 + t * 1.330274))));
    return Math.max(0, Math.min(1, 2 * p));
  }

  function strength(r) {
    var a = Math.abs(r);
    if (a < 0.1) { return 'effectively none'; }
    if (a < 0.3) { return 'weak'; }
    if (a < 0.5) { return 'moderate'; }
    if (a < 0.7) { return 'strong'; }
    return 'very strong';
  }

  function nice(v) {
    if (v === null || !isFinite(v)) { return '--'; }
    var a = Math.abs(v);
    if (a >= 100) { return v.toFixed(0); }
    if (a >= 10) { return v.toFixed(1); }
    if (a >= 1) { return v.toFixed(2); }
    return v.toFixed(3);
  }

  function draw() {
    var xi = LL_FIELDS.indexOf(xSel.value);
    var yi = LL_FIELDS.indexOf(ySel.value);
    var which = scaleSel.value === 'raw' ? 4 : 5;
    var rows = rowsInView();

    var pts = [], labels = [];
    for (var i = 0; i < rows.length; i++) {
      var xv = rows[i][which][xi], yv = rows[i][which][yi];
      if (xv === null || yv === null) { continue; }
      pts.push([xv, yv]);
      labels.push(rows[i]);
    }

    if (pts.length < 3) {
      plot.innerHTML = '<p class="note">Not enough points to plot.</p>';
      stats.textContent = '';
      return;
    }

    var xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
    pts.forEach(function (p) {
      if (p[0] < xmin) { xmin = p[0]; } if (p[0] > xmax) { xmax = p[0]; }
      if (p[1] < ymin) { ymin = p[1]; } if (p[1] > ymax) { ymax = p[1]; }
    });
    if (xmax === xmin) { xmax = xmin + 1; }
    if (ymax === ymin) { ymax = ymin + 1; }
    var xpad = (xmax - xmin) * 0.05, ypad = (ymax - ymin) * 0.05;
    xmin -= xpad; xmax += xpad; ymin -= ypad; ymax += ypad;

    var sx = function (v) { return PL + IW * (v - xmin) / (xmax - xmin); };
    var sy = function (v) { return PT + IH * (1 - (v - ymin) / (ymax - ymin)); };

    var out = ['<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="'
               + 'Correlation between ' + xSel.value + ' and ' + ySel.value + '">'];
    var k;
    for (k = 0; k <= 4; k++) {
      var gx = PL + IW * k / 4, gy = PT + IH * k / 4;
      out.push('<line x1="' + gx.toFixed(1) + '" y1="' + PT + '" x2="'
               + gx.toFixed(1) + '" y2="' + (PT + IH) + '" stroke="var(--grid)"/>');
      out.push('<line x1="' + PL + '" y1="' + gy.toFixed(1) + '" x2="' + (PL + IW)
               + '" y2="' + gy.toFixed(1) + '" stroke="var(--grid)"/>');
      out.push('<text x="' + gx.toFixed(1) + '" y="' + (PT + IH + 16)
               + '" text-anchor="middle" font-size="11" fill="var(--muted)">'
               + nice(xmin + (xmax - xmin) * k / 4) + '</text>');
      out.push('<text x="' + (PL - 8) + '" y="' + (gy + 4).toFixed(1)
               + '" text-anchor="end" font-size="11" fill="var(--muted)">'
               + nice(ymax - (ymax - ymin) * k / 4) + '</text>');
    }

    for (k = 0; k < pts.length; k++) {
      out.push('<circle cx="' + sx(pts[k][0]).toFixed(1) + '" cy="'
               + sy(pts[k][1]).toFixed(1) + '" r="3.4" fill="var(--accent)"'
               + ' fill-opacity=".45"><title>' + labels[k][0] + ' LL'
               + labels[k][2] + ' (' + labels[k][3] + ')</title></circle>');
    }

    var f = fit(pts);
    if (f && lineBox.checked) {
      var x1 = xmin, x2 = xmax;
      var y1 = f.intercept + f.slope * x1, y2 = f.intercept + f.slope * x2;
      // Clip the drawn line to the plot box rather than letting a steep fit
      // run off and stretch the viewBox.
      var cy1 = Math.max(ymin, Math.min(ymax, y1));
      var cy2 = Math.max(ymin, Math.min(ymax, y2));
      if (f.slope !== 0) {
        if (cy1 !== y1) { x1 = (cy1 - f.intercept) / f.slope; }
        if (cy2 !== y2) { x2 = (cy2 - f.intercept) / f.slope; }
      }
      out.push('<line x1="' + sx(x1).toFixed(1) + '" y1="' + sy(cy1).toFixed(1)
               + '" x2="' + sx(x2).toFixed(1) + '" y2="' + sy(cy2).toFixed(1)
               + '" stroke="var(--warm)" stroke-width="2" opacity=".9"/>');
    }

    out.push('<text x="' + (PL + IW / 2) + '" y="' + (H - 8)
             + '" text-anchor="middle" font-size="12" fill="var(--muted)">'
             + xSel.options[xSel.selectedIndex].text + '</text>');
    out.push('<text transform="translate(14,' + (PT + IH / 2)
             + ') rotate(-90)" text-anchor="middle" font-size="12"'
             + ' fill="var(--muted)">' + ySel.options[ySel.selectedIndex].text
             + '</text>');
    out.push('</svg>');
    plot.innerHTML = out.join('');

    if (!f) {
      stats.textContent = 'No spread on one of the axes, so no fit.';
      return;
    }
    var p = pValue(f.r, f.n);
    var bits = [
      '<b>n</b> ' + f.n,
      '<b>r</b> ' + f.r.toFixed(3),
      '<b>R&sup2;</b> ' + (f.r * f.r).toFixed(3),
      '<b>slope</b> ' + nice(f.slope)
    ];
    if (p !== null) {
      bits.push('<b>p</b> ' + (p < 0.001 ? '&lt;0.001' : p.toFixed(3)));
    }
    var read = strength(f.r) + (Math.abs(f.r) < 0.1 ? '' :
               (f.r > 0 ? ', positive' : ', negative'));
    bits.push('<span class="dim">' + read + '</span>');
    stats.innerHTML = bits.join('<span class="sep">&middot;</span>');
  }

  [xSel, ySel, scaleSel, lineBox].forEach(function (el) {
    el.addEventListener('change', draw);
  });
  draw();
  return { draw: draw };
})();

var themer = document.getElementById('theme');
if (themer) {
  var modes = ['auto', 'light', 'dark'], at = 0;
  try { at = Math.max(0, modes.indexOf(localStorage.getItem('llTheme'))); }
  catch (e) { at = 0; }
  var paint = function () {
    var m = modes[at];
    if (m === 'auto') { document.documentElement.removeAttribute('data-theme'); }
    else { document.documentElement.setAttribute('data-theme', m); }
    themer.textContent = 'theme: ' + m;
    try { localStorage.setItem('llTheme', m); } catch (e) {}
  };
  paint();
  themer.addEventListener('click', function () {
    at = (at + 1) % modes.length; paint();
  });
}
"""


def esc(text):
    """
    Escape a value for html output.
    """
    return html.escape(str(text))


def fmt(value, places=1, dash='--'):
    """
    Format a number that may be missing.
    """
    if value is None:
        return dash
    return '%.*f' % (places, value)


def bar_cell(pct):
    """
    A percentile as a small bar with the number sitting on it.
    """
    if pct is None:
        return '<td class="dim" data-sort="-1">--</td>'
    return ('<td data-sort="%.2f"><span class="bar"><i style="width:%.1f%%">'
            '</i><span>%.0f</span></span></td>'
            % (pct, max(2.0, min(100.0, pct)), pct))


def level_pill(level):
    """
    Rundle level as a coloured chip.
    """
    if not level:
        return '<span class="dim">--</span>'
    return ('<span class="pill" style="background:%s">%s</span>'
            % (LEVEL_COLOUR.get(level, '#888'), esc(level)))


def signed(value, places=0):
    """
    A signed number coloured by direction.
    """
    if value is None:
        return '<td class="dim" data-sort="0">--</td>'
    return ('<td class="%s" data-sort="%.3f">%+.*f</td>'
            % ('up' if value >= 0 else 'down', value, places, value))


def section(sid, title, note, body):
    """
    Wrap a block in a foldable section.
    """
    return ('<section id="%s"><h2>%s<span class="fold">hide</span></h2>'
            '<p class="note">%s</p>%s</section>'
            % (sid, esc(title), note, body))


def render_awards(awards):
    """
    The superlatives grid.
    """
    if not awards:
        return ''
    cards = ''.join(
        '<div class="card"><div class="label">%s</div><div class="who">%s</div>'
        '<div class="val">%s</div><div class="why">%s</div></div>'
        % (esc(t), esc(w), esc(v), esc(n)) for t, w, v, n in awards)
    return section('awards', 'Superlatives',
                   'Season honours and career honours side by side.',
                   '<div class="cards">%s</div>' % cards)


def render_standings(current, season, matchday):
    """
    Where everyone stands in the season on show.
    """
    if not current:
        return ''
    head = ('<tr><th>Player</th><th>Group</th><th>Rundle</th><th>Lvl</th>'
            '<th>Rank</th>'
            '<th>W-L-T</th><th>Pts</th><th>TCA</th><th>QPct %</th><th>MPD</th>'
            '<th>Pts pct</th><th>QPct pct</th><th>Conv</th></tr>')
    body = []
    for ent in current:
        body.append(
            '<tr><td class="name">%s</td><td class="dim">%s</td>'
            '<td class="dim">%s</td><td>%s</td>'
            '<td data-sort="%d">%d</td><td>%s</td>'
            '<td data-sort="%s">%s</td><td data-sort="%s">%s</td>'
            '<td data-sort="%s">%s</td><td data-sort="%s">%s</td>%s%s%s</tr>'
            % (esc(ent['username']), esc(ent.get('group', '')),
               esc(ent['rundle']),
               level_pill(ent['level']), ent['rank'], ent['rank'],
               esc(ent['record']),
               fmt(ent['values'].get('Pts'), 0, '0'),
               fmt(ent['values'].get('Pts'), 0),
               fmt(ent['values'].get('TCA'), 0, '0'),
               fmt(ent['values'].get('TCA'), 0),
               fmt(ent['values'].get('QPct'), 4, '0'),
               fmt((ent['values'].get('QPct') or 0) * 100, 1),
               fmt(ent['values'].get('MPD'), 0, '0'),
               fmt(ent['values'].get('MPD'), 0),
               bar_cell(ent['pcts'].get('Pts')),
               bar_cell(ent['pcts'].get('QPct')),
               signed(ent['conversion'])))
    return section(
        'standings', 'Season %d standings' % season,
        'Through match day %d. Percentile columns compare each player with '
        'everyone else at the same level that season, so a D rundle number '
        'and an A rundle number can be read side by side. Every heading '
        'sorts.' % matchday,
        '<div class="scroll"><table class="sortable"><thead>%s</thead>'
        '<tbody>%s</tbody></table></div>' % (head, '\n'.join(body)))


def render_groups(careers, current, order):
    """
    A row per roster group.

    Following a cohort is a different question from following a player, so
    this rolls each group up.  Because the groups sit at different levels --
    a set of recent referrals against a set of long-serving Arcadians -- the
    median level percentile is the column to compare on, not median points.
    """
    if len(order) < 2:
        return ''
    playing = {}
    for ent in current:
        playing.setdefault(ent.get('group', DEFAULT_GROUP), []).append(ent)
    rows = []
    for name in order:
        members = [car for car in careers if car['group'] == name]
        active = playing.get(name, [])
        idle = [car['username'] for car in members
                if car.get('found') and car['username'] not in
                {e['username'] for e in active}]
        unseen = [car['username'] for car in members if not car.get('found')]
        pcts = [ent['pcts']['Pts'] for ent in active
                if ent['pcts'].get('Pts') is not None]
        careers_pct = [car['mean_pct'] for car in members
                       if car.get('found') and car['mean_pct'] is not None]
        seasons = [car['count'] for car in members if car.get('found')]
        best = (max(active, key=lambda e: e['values'].get('Pts') or -999)
                if active else None)
        note = []
        if idle:
            note.append('%d sitting out' % len(idle))
        if unseen:
            note.append('%d never seen' % len(unseen))
        rows.append(
            '<tr><td class="name">%s</td>'
            '<td data-sort="%d">%d</td><td data-sort="%d">%d</td>'
            '<td data-sort="%.1f">%s</td><td data-sort="%.1f">%s</td>'
            '<td data-sort="%.1f">%s</td><td>%s</td>'
            '<td class="dim">%s</td></tr>'
            % (esc(name), len(members), len(members), len(active),
               len(active),
               statistics.median(pcts) if pcts else -1,
               fmt(statistics.median(pcts), 0) if pcts else '--',
               statistics.median(careers_pct) if careers_pct else -1,
               fmt(statistics.median(careers_pct), 0) if careers_pct else '--',
               statistics.median(seasons) if seasons else -1,
               fmt(statistics.median(seasons), 0) if seasons else '--',
               esc(best['username']) if best else '--',
               esc(', '.join(note)) or '&mdash;'))
    return section(
        'groups', 'By group',
        'Groups sit at very different points in their careers, so compare '
        'them on the percentile columns rather than on raw points. "Playing" '
        'counts those in the current season; the last column notes members '
        'sitting this one out or absent from every export.',
        '<div class="scroll"><table class="sortable"><thead><tr>'
        '<th>Group</th><th>Members</th><th>Playing</th>'
        '<th>Median pts pct now</th><th>Median career pct</th>'
        '<th>Median seasons</th><th>Top this season</th><th>Absences</th>'
        '</tr></thead><tbody>%s</tbody></table></div>' % '\n'.join(rows))


def render_referrals(careers):
    """
    The referral tree: who brought whom in, and how those recruits are
    doing measured against their own levels.
    """
    kids = {}
    for car in careers:
        if car.get('referrer'):
            kids.setdefault(car['referrer'], []).append(car)
    if not kids:
        return ''
    by_name = {car['username']: car for car in careers}
    rows = []
    for parent in sorted(kids, key=lambda p: -len(kids[p])):
        brood = sorted(kids[parent], key=lambda c: c['username'].lower())
        played = [car for car in brood if car.get('found')]
        pcts = [car['mean_pct'] for car in played
                if car['mean_pct'] is not None]
        top = max(played, key=lambda c: c['mean_pct'] or -1) if played else None
        own = by_name.get(parent)
        rows.append(
            '<tr><td class="name">%s</td><td data-sort="%.1f">%s</td>'
            '<td data-sort="%d">%d</td><td data-sort="%.1f">%s</td>'
            '<td>%s</td><td class="dim">%s</td></tr>'
            % (esc(parent),
               (own['mean_pct'] if own and own.get('found')
                and own['mean_pct'] is not None else -1),
               fmt(own['mean_pct'], 0) if own and own.get('found') else '--',
               len(brood), len(brood),
               statistics.mean(pcts) if pcts else -1,
               fmt(statistics.mean(pcts), 0) if pcts else '--',
               esc(top['username']) if top else '--',
               esc(', '.join(car['username'] for car in brood))))
    return section(
        'referrals', 'Referral tree',
        'Who brought whom into the league, and how that intake has fared. '
        'Recruits are compared on mean career percentile within their own '
        'level, so a rookie-rundle newcomer and a C rundle veteran can sit '
        'in the same column.',
        '<div class="scroll"><table class="sortable"><thead><tr>'
        '<th>Referrer</th><th>Their career pct</th><th>Recruits</th>'
        '<th>Mean recruit pct</th><th>Best recruit</th><th>Who</th>'
        '</tr></thead><tbody>%s</tbody></table></div>' % '\n'.join(rows))


def sparkline(entries, lo_season, hi_season, width=150, height=26):
    """
    Points percentile across a career, drawn small enough to live in a table
    cell.  x is the season, so gap years show as gaps and everyone's line
    shares one time axis.
    """
    points = [(ent['season'], ent['pcts']['Pts']) for ent in entries
              if ent['pcts'].get('Pts') is not None]
    if len(points) < 2:
        return '<span class="dim">--</span>'
    span = max(1, hi_season - lo_season)
    def place(season, pct):
        """
        Season and percentile to svg coordinates.
        """
        return (2 + (width - 4) * (season - lo_season) / span,
                2 + (height - 4) * (1.0 - pct / 100.0))
    runs, run = [], []
    for i, (season, pct) in enumerate(points):
        if i and season - points[i - 1][0] != 1:
            runs.append(run)
            run = []
        run.append(place(season, pct))
    runs.append(run)
    paths = ''.join(
        '<polyline fill="none" stroke="var(--accent)" stroke-width="1.5" '
        'points="%s"/>' % ' '.join('%.1f,%.1f' % pt for pt in seg)
        for seg in runs if len(seg) > 1)
    dots = ''.join('<circle cx="%.1f" cy="%.1f" r="1.6" fill="var(--accent)"/>'
                   % seg[-1] for seg in runs if seg)
    mid = 2 + (height - 4) * 0.5
    return ('<svg viewBox="0 0 %d %d" width="%d" height="%d">'
            '<line x1="0" y1="%.1f" x2="%d" y2="%.1f" stroke="var(--grid)" '
            'stroke-width="1"/>%s%s</svg>'
            % (width, height, width, height, mid, width, mid, paths, dots))


def level_strip(entries, lo_season, hi_season, cell=11, height=15):
    """
    A career as a row of coloured blocks, one per season, so promotions and
    relegations read at a glance.
    """
    width = (hi_season - lo_season + 1) * cell
    by_season = {ent['season']: ent for ent in entries}
    blocks = []
    for i, season in enumerate(range(lo_season, hi_season + 1)):
        ent = by_season.get(season)
        if ent is None:
            blocks.append('<rect x="%d" y="6" width="%d" height="3" rx="1" '
                          'fill="var(--grid)"/>' % (i * cell, cell - 2))
            continue
        blocks.append('<rect x="%d" y="0" width="%d" height="%d" rx="2" '
                      'fill="%s"><title>LL%d %s</title></rect>'
                      % (i * cell, cell - 2, height,
                         LEVEL_COLOUR.get(ent['level'], '#888'),
                         season, esc(ent['rundle'])))
    return ('<svg viewBox="0 0 %d %d" width="%d" height="%d">%s</svg>'
            % (width, height, width, height, ''.join(blocks)))


def render_careers(careers, lo, hi):
    """
    The career table: the long view of every player on the roster.
    """
    played = [car for car in careers if car.get('found')]
    if not played:
        return ''
    played.sort(key=lambda car: -(car['mean_pct'] or 0))
    head = ('<tr><th>Player</th><th>Group</th><th>Seasons</th><th>Span</th>'
            '<th>Level history</th><th>Career W-L-T</th><th>Win pct</th>'
            '<th>Mean lvl pct</th><th>Trajectory</th><th>Peak</th>'
            '<th>Titles</th><th>Up/Dn</th><th>Spread</th><th>Swing</th></tr>')
    body = []
    for car in played:
        body.append(
            '<tr><td class="name">%s</td><td class="dim">%s</td>'
            '<td data-sort="%d">%d</td>'
            '<td class="dim" data-sort="%d">%d&ndash;%d</td>'
            '<td>%s</td>'
            '<td>%d-%d-%d</td>'
            '<td data-sort="%.4f">%.3f</td>'
            '<td data-sort="%.2f">%s</td>'
            '<td>%s</td>'
            '<td>%s</td>'
            '<td data-sort="%d">%s</td>'
            '<td class="dim">%d/%d</td>'
            '<td data-sort="%.2f">%s</td>%s</tr>'
            % (esc(car['username']), esc(car['group']),
               car['count'], car['count'],
               car['first'], car['first'], car['last'],
               level_strip(car['seasons'], lo, hi),
               car['wins'], car['losses'], car['ties'],
               car['win_pct'], car['win_pct'],
               car['mean_pct'] or 0, fmt(car['mean_pct'], 0),
               sparkline(car['seasons'], lo, hi),
               level_pill(car['peak_level']),
               car['titles'], car['titles'] or '<span class="dim">0</span>',
               car['promotions'], car['relegations'],
               car['spread'] or 0, fmt(car['spread'], 0),
               signed(car['swing'])))
    legend = ' '.join('%s&nbsp;%s' % (level_pill(lvl), '') for lvl in LEVELS)
    return section(
        'careers', 'Careers',
        'Every season on record for each player, LL%d to LL%d. The level '
        'history is one block per season (%s gaps mean a season not played); '
        'the trajectory line is points percentile within level over time, '
        'with the midline at the 50th. Spread and swing are defined in the '
        'glossary.' % (lo, hi, legend),
        '<div class="scroll"><table class="sortable"><thead>%s</thead>'
        '<tbody>%s</tbody></table></div>' % (head, '\n'.join(body)))


def render_levels(careers, lo, hi):
    """
    A grid of season against player, coloured by level -- the group's whole
    history on one plate.
    """
    played = [car for car in careers if car.get('found')]
    if not played:
        return ''
    played.sort(key=lambda car: (car['first'], car['username'].lower()))
    seasons = list(range(lo, hi + 1))
    head = ['<tr><th>Player</th>']
    for season in seasons:
        head.append('<th style="font-size:9.5px">%d</th>' % season)
    head.append('</tr>')
    body = []
    for car in played:
        by_season = {ent['season']: ent for ent in car['seasons']}
        cells = ['<tr><td class="name">%s</td>' % esc(car['username'])]
        for season in seasons:
            ent = by_season.get(season)
            if ent is None:
                cells.append('<td class="dim">&middot;</td>')
                continue
            pct = ent['pcts'].get('Pts')
            cells.append(
                '<td data-sort="%.1f" title="LL%d %s, rank %d" '
                'style="background:%s;color:#fff;font-size:10.5px">%s</td>'
                % (pct or 0, season, esc(ent['rundle']), ent['rank'],
                   LEVEL_COLOUR.get(ent['level'], '#888'), esc(ent['level'])))
        cells.append('</tr>')
        body.append(''.join(cells))
    return section(
        'levels', 'Level history',
        'One cell per player per season, coloured by rundle level. Hover a '
        'cell for the rundle and finishing rank. Rows are ordered by debut.',
        '<div class="scroll"><table><thead>%s</thead><tbody>%s</tbody>'
        '</table></div>' % (''.join(head), '\n'.join(body)))


CHAR_W = 7.0
LABEL_H = 15.0
PAD = 3.0


def place_label(text, xpos, ypos, right_edge, placed):
    """
    Find somewhere near a point to put its label without landing on one
    already down.  Slots are tried beside the point first, then further
    above and below.
    """
    width = len(text) * CHAR_W
    slots = []
    #
    # Candidate offsets fan out both sideways and vertically, tried nearest
    # first so a label only travels as far as it has to.  Points in a dense
    # cluster need the sideways lanes: nudging every one of them up and
    # down alone runs out of room.
    #
    offsets = [(dxpos, dypos)
               for dxpos in (9, -9, 34, -34, 62, -62, 92, -92, 124, -124)
               for dypos in (4, -10, 17, -22, 29, -34, 41, -46, 53, -58,
                             66, -71)]
    offsets.sort(key=lambda off: abs(off[0]) + abs(off[1]))
    for dxpos, dypos in offsets:
        anchor = 'start' if dxpos > 0 else 'end'
        if anchor == 'start' and xpos + dxpos + width > right_edge:
            continue
        left = xpos + dxpos if anchor == 'start' else xpos + dxpos - width
        box = (left - PAD, ypos + dypos - LABEL_H - PAD,
               left + width + PAD, ypos + dypos + PAD)
        slots.append((box, xpos + dxpos, ypos + dypos, anchor))
    if not slots:
        return xpos - 9, ypos + 4, 'end'
    for box, text_x, text_y, anchor in slots:
        if not any(box[0] < o[2] and o[0] < box[2] and
                   box[1] < o[3] and o[1] < box[3] for o in placed):
            placed.append(box)
            return text_x, text_y, anchor
    placed.append(slots[0][0])
    return slots[0][1], slots[0][2], slots[0][3]


def render_scatter(current, season):
    """
    Knowledge against results, both as within-level percentiles.
    """
    def colour_of(ent):
        """
        Green where points ran ahead of the raw question count.
        """
        return ('var(--accent)' if (ent['conversion'] or 0) >= 0
                else 'var(--warm)')

    chart = pct_scatter(
        current, 'TCA', 'Pts',
        'Correct answers, percentile within level',
        'Standings points, percentile within level',
        'points match knowledge', colour_of, 'Knowledge against results')
    if not chart:
        return ''
    return section(
        'scatter', 'Knowledge against results, LL%d' % season,
        'Both axes are percentiles inside a player&rsquo;s own level. Above the '
        'dashed line means more standings points than the raw question count '
        'predicts; below it means the opposite.', chart)


def pct_scatter(entries, x_field, y_field, x_label, y_label, caption,
                colour_of, aria, width=720, height=470):
    """
    A scatter of two percentiles with the parity diagonal drawn in.

    Both axes are 0-100 within-level percentiles, so the diagonal is the
    line where a player rates the same on each. Callers supply the colour
    rule, which is what carries the meaning: on the knowledge plot it marks
    whether points ran ahead of the question count, on the offence/defence
    plot which half of the game a player wins on.
    """
    plotted = [e for e in entries if e['pcts'].get(x_field) is not None
               and e['pcts'].get(y_field) is not None]
    if not plotted:
        return ''
    pad_l, pad_r, pad_t, pad_b = 58, 22, 18, 46
    inner_w, inner_h = width - pad_l - pad_r, height - pad_t - pad_b

    def pos(xpct, ypct):
        """
        Percentile pair to svg coordinates.
        """
        return (pad_l + inner_w * xpct / 100.0,
                pad_t + inner_h * (1.0 - ypct / 100.0))

    parts = ['<svg viewBox="0 0 %d %d" role="img" aria-label="%s">'
             % (width, height, esc(aria))]
    for tick in range(0, 101, 25):
        xpos, _ = pos(tick, 0)
        _, ypos = pos(0, tick)
        parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" '
                     'stroke="var(--grid)"/>'
                     % (xpos, pad_t, xpos, pad_t + inner_h))
        parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" '
                     'stroke="var(--grid)"/>'
                     % (pad_l, ypos, pad_l + inner_w, ypos))
        parts.append('<text x="%.1f" y="%d" text-anchor="middle" '
                     'font-size="11" fill="var(--muted)">%d</text>'
                     % (xpos, pad_t + inner_h + 16, tick))
        parts.append('<text x="%d" y="%.1f" text-anchor="end" font-size="11" '
                     'fill="var(--muted)">%d</text>' % (pad_l - 8, ypos + 4,
                                                        tick))
    start, end = pos(0, 0), pos(100, 100)
    parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" '
                 'stroke="var(--muted)" stroke-dasharray="5 4" '
                 'opacity=".7"/>' % (start[0], start[1], end[0], end[1]))
    cap_x, cap_y = pad_l + inner_w - 168, pad_t + 14
    parts.append('<text x="%.1f" y="%.1f" font-size="11" fill="var(--muted)">'
                 '%s</text>' % (cap_x, cap_y, esc(caption)))
    #
    # The caption and both sets of axis numbers go down first, so the point
    # labels treat them as taken and route around them.
    #
    placed = [(cap_x, cap_y - LABEL_H, cap_x + len(caption) * CHAR_W, cap_y)]
    for tick in range(0, 101, 25):
        xpos, _ = pos(tick, 0)
        _, ypos = pos(0, tick)
        placed.append((xpos - 12, pad_t + inner_h + 4,
                       xpos + 12, pad_t + inner_h + 20))
        placed.append((pad_l - 26, ypos - 8, pad_l - 4, ypos + 8))
    for ent in plotted:
        xpos, ypos = pos(ent['pcts'][x_field], ent['pcts'][y_field])
        parts.append('<circle cx="%.1f" cy="%.1f" r="5.5" fill="%s" '
                     'fill-opacity=".85"/>' % (xpos, ypos, colour_of(ent)))
        tx, ty, anchor = place_label(ent['username'], xpos, ypos,
                                     pad_l + inner_w, placed)
        parts.append('<text x="%.1f" y="%.1f" text-anchor="%s" font-size="11" '
                     'fill="var(--ink)">%s</text>'
                     % (tx, ty, anchor, esc(ent['username'])))
    parts.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="12" '
                 'fill="var(--muted)">%s</text>'
                 % (pad_l + inner_w / 2.0, height - 8, esc(x_label)))
    parts.append('<text transform="translate(14,%.1f) rotate(-90)" '
                 'text-anchor="middle" font-size="12" fill="var(--muted)">'
                 '%s</text>' % (pad_t + inner_h / 2.0, esc(y_label)))
    parts.append('</svg>')
    return '\n'.join(parts)


def rating_cell(field, pct):
    """
    A percentile bar, flipped for the fields where low is the good outcome
    so that a long bar always means a good result.
    """
    if pct is None:
        return bar_cell(None)
    return bar_cell(100.0 - pct if field in LOWER_IS_BETTER else pct)


def render_profile(current, season):
    """
    Offence against defence.

    A LearnedLeague match asks two separate things: answer six questions,
    and decide how many points each of the opponent's questions is worth.
    A player can be strong at one and weak at the other, and the standings
    alone will not say which. This separates them.
    """
    if not current:
        return ''

    def colour_of(ent):
        """
        Green where defence leads, warm where offence does.
        """
        off = ent['pcts'].get('OE') or 0
        dfn = ent['pcts'].get('DE') or 0
        return 'var(--accent)' if dfn >= off else 'var(--warm)'

    chart = pct_scatter(
        current, 'OE', 'DE',
        'Offensive efficiency, percentile within level',
        'Defensive efficiency, percentile within level',
        'balanced', colour_of, 'Offence against defence')

    cols = [('PCA', 'PCA', 'Match points won per correct answer'),
            ('PCAA', 'PCAA', 'Points conceded per opponent correct answer'),
            ('UfPE', 'UfPE', 'Unforced points earned'),
            ('UfPA', 'UfPA', 'Unforced points allowed'),
            ('NUfP', 'NUfP', 'Net unforced points')]
    head = ['<tr><th>Player</th><th>Lvl</th><th>OE</th><th>DE</th>']
    for _, label, title in cols:
        head.append('<th title="%s">%s</th>' % (esc(title), esc(label)))
    head.append('</tr>')

    body = []
    for ent in sorted(current, key=lambda e: -(e['pcts'].get('DE') or 0)):
        cells = ['<tr><td class="name">%s</td><td>%s</td>'
                 % (esc(ent['username']), level_pill(ent['level']))]
        cells.append(rating_cell('OE', ent['pcts'].get('OE')))
        cells.append(rating_cell('DE', ent['pcts'].get('DE')))
        for field, _, _ in cols:
            value = ent['values'].get(field)
            cells.append('<td data-sort="%s">%s</td>'
                         % ('' if value is None else '%.4f' % value,
                            '--' if value is None
                            else ('%.2f' % value if field in ('PCA', 'PCAA')
                                  else '%d' % round(value))))
        cells.append('</tr>')
        body.append(''.join(cells))

    return section(
        'profile', 'Offence and defence, LL%d' % season,
        'A match is two jobs: answer six questions, and set what each of the '
        'opponent&rsquo;s questions is worth. Points earned per correct '
        'answer (PCA) measures the first, points conceded per opponent '
        'correct answer (PCAA) the second. Unforced points are those handed '
        'over by misjudging where an opponent was strong; net unforced '
        'points is what a player gained this way minus what they gave away. '
        'Bars are percentiles within level, drawn so that longer is always '
        'better.',
        chart + '<div class="scroll"><table class="sortable"><thead>%s</thead>'
        '<tbody>%s</tbody></table></div>' % (''.join(head), '\n'.join(body)))


def render_record(current, season):
    """
    Whether the win-loss column matches the match points behind it.

    Matches are won on the day by margin, and a heavy win pays exactly what
    a narrow one does. Comparing a player's standings-points percentile with
    their match-point-differential percentile separates the two.
    """
    plotted = [e for e in current if e.get('margin') is not None]
    if not plotted:
        return ''
    plotted = sorted(plotted, key=lambda e: -(e['margin'] or 0))
    span = max(abs(e['margin']) for e in plotted) or 1.0

    rows = []
    for ent in plotted:
        margin = ent['margin']
        width = 46.0 * abs(margin) / span
        left = 50.0 - width if margin < 0 else 50.0
        colour = 'var(--accent)' if margin >= 0 else 'var(--warm)'
        forfeits = ent['values'].get('FL') or 0
        rows.append(
            '<tr><td class="name">%s</td><td>%s</td>'
            '<td data-sort="%.2f"><span class="diverge">'
            '<i style="left:%.1f%%;width:%.1f%%;background:%s"></i>'
            '<span class="mid"></span></span></td>'
            '<td data-sort="%.2f">%+.0f</td>'
            '<td data-sort="%.2f">%.0f</td><td data-sort="%.2f">%.0f</td>'
            '<td data-sort="%d">%s</td></tr>'
            % (esc(ent['username']), level_pill(ent['level']),
               margin, left, max(width, 1.0), colour,
               margin, margin,
               ent['pcts'].get('Pts') or 0, ent['pcts'].get('Pts') or 0,
               ent['pcts'].get('MPD') or 0, ent['pcts'].get('MPD') or 0,
               forfeits,
               '--' if not forfeits else '%d (-%d pts)' % (forfeits, forfeits)))

    return section(
        'record', 'Record against match points, LL%d' % season,
        'Match points are what a player actually scored across the season; '
        'standings points are what the win-loss column paid them. A match is '
        'settled by margin, so a heavy win and a narrow one are worth the '
        'same, and the two can drift apart. Green means the record flatters '
        'the underlying scoring, warm means it understates it. Forfeit '
        'losses are shown because they are charged twice: the match is lost '
        'and a further standings point is deducted.',
        '<div class="scroll"><table class="sortable"><thead><tr>'
        '<th>Player</th><th>Lvl</th><th>Record vs scoring</th><th>Gap</th>'
        '<th>Pts pct</th><th>MPD pct</th><th>Forfeits</th></tr></thead>'
        '<tbody>%s</tbody></table></div>' % '\n'.join(rows))


def render_outlook(current, season, matchday):
    """
    Where each player sits in the rundle they actually play in.

    Percentiles compare a player against everyone at their level league
    wide, which is the fair way to rate a season but says nothing about
    promotion, because that is settled inside one rundle of twenty-odd
    people. This reads the rundle's own table.

    Promotion and relegation lines vary by rundle and are not in the export,
    so this reports gaps and leaves the verdict alone.
    """
    rows = []
    for ent in sorted(current, key=lambda e: (e['level_ix'] if
                                              e['level_ix'] is not None else 9,
                                              e['rank'] or 99)):
        field = ent.get('rundle_field') or []
        pts = ent['values'].get('Pts')
        if not field or pts is None or not ent['rank']:
            continue
        leader = field[0]
        second = field[1] if len(field) > 1 else leader
        last = field[-1]
        rows.append(
            '<tr><td class="name">%s</td><td>%s</td><td class="dim">%s</td>'
            '<td data-sort="%d">%d of %d</td>'
            '<td data-sort="%.0f">%.0f</td>'
            '<td data-sort="%.0f">%s</td>'
            '<td data-sort="%.0f">%s</td>'
            '<td data-sort="%.0f">%s</td></tr>'
            % (esc(ent['username']), level_pill(ent['level']),
               esc(ent['rundle']),
               ent['rank'], ent['rank'], len(field),
               pts, pts,
               leader - pts,
               'leads' if pts >= leader else '%.0f back' % (leader - pts),
               second - pts,
               'top two' if pts >= second else '%.0f back' % (second - pts),
               pts - last,
               '--' if pts <= last else '%.0f clear' % (pts - last)))

    if not rows:
        return ''
    return section(
        'outlook', 'Position in rundle, LL%d through match day %d'
        % (season, matchday),
        'Everything else on this page rates a player against their whole '
        'level. Promotion is decided somewhere narrower: inside one rundle '
        'of twenty to thirty-odd people. These are the gaps to the top of '
        'that table and to the bottom of it. The cut lines themselves vary '
        'by rundle and are not carried in the export, so they are not '
        'guessed at here.',
        '<div class="scroll"><table class="sortable"><thead><tr>'
        '<th>Player</th><th>Lvl</th><th>Rundle</th><th>Position</th>'
        '<th>Pts</th><th>To first</th><th>To second</th><th>Above last</th>'
        '</tr></thead><tbody>%s</tbody></table></div>' % '\n'.join(rows))


def explorer_payload(careers):
    """
    Every player-season the roster has, flattened for the explorer.

    Stored as parallel arrays rather than objects: this is the one block on
    the page that grows with seasons times players times metrics, and the
    key names would otherwise be repeated some twenty thousand times.
    """
    fields = [field for field, _, _, _ in ALL_METRICS]
    rows = []
    for car in careers:
        if not car.get('found'):
            continue
        for ent in car['seasons']:
            values, pcts = [], []
            for field in fields:
                value = ent['values'].get(field)
                pct = ent['pcts'].get(field)
                values.append(None if value is None else round(value, 4))
                pcts.append(None if pct is None else round(pct, 1))
            rows.append([car['username'], car['group'], ent['season'],
                         ent['level'], values, pcts])
    return fields, rows


def render_explorer(careers):
    """
    Pick any two measures and see whether they move together.

    Every other plate on the page answers a question someone already chose.
    This one does not: it is the tool for the question nobody thought to
    ask, which on a data set this shape is usually the interesting one.

    A career is the unit, not a player, because a roster of forty-six is too
    thin to read a correlation from. Twenty-five seasons of them is not.
    """
    fields, rows = explorer_payload(careers)
    if len(rows) < 8:
        return ''

    labels = {'Pts': 'Standings points', 'QPct': 'Question percentage',
              'TCA': 'Total correct answers', 'MPD': 'Match point differential',
              'OE': 'Offensive efficiency', 'DE': 'Defensive efficiency',
              'PCA': 'Points per correct answer',
              'PCAA': 'Points conceded per opponent correct',
              'UfPE': 'Unforced points earned',
              'UfPA': 'Unforced points allowed',
              'NUfP': 'Net unforced points', 'FL': 'Forfeit losses'}

    def options(selected):
        """
        One <option> per measure, marking the default.
        """
        out = []
        for field in fields:
            out.append('<option value="%s"%s>%s</option>'
                       % (esc(field), ' selected' if field == selected else '',
                          esc(labels.get(field, field))))
        return ''.join(out)

    controls = (
        '<div class="ex-controls">'
        '<label>x <select id="ex-x">%s</select></label>'
        '<label>y <select id="ex-y">%s</select></label>'
        '<label>as <select id="ex-scale">'
        '<option value="pct">percentile within level</option>'
        '<option value="raw">raw value</option>'
        '</select></label>'
        '<label><input type="checkbox" id="ex-line" checked> trend line</label>'
        '</div>' % (options('QPct'), options('Pts')))

    return section(
        'explorer', 'Correlation explorer',
        'One point per player-season, %d of them across LL%d&ndash;LL%d. '
        'Pick a measure for each axis and the fit is recomputed underneath. '
        'Percentiles compare a player against their own level that season, '
        'which is the honest way to put an A rundle season next to an E; raw '
        'values do not, and will show level rather than skill as a result. '
        'The filter box and group picker at the top of the page apply here '
        'too. Correlation is not cause, and with this many points a small r '
        'will still look like a pattern.'
        % (len(rows), min(r[2] for r in rows), max(r[2] for r in rows)),
        controls
        + '<div id="ex-plot"></div>'
        + '<div id="ex-stats" class="ex-stats"></div>')


def ordinal(number):
    """
    1st, 2nd, 3rd, 4th -- with the teens, which all take 'th'.
    """
    number = int(number)
    if 10 <= number % 100 <= 20:
        suffix = 'th'
    else:
        suffix = {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')
    return '%d%s' % (number, suffix)


def render_summary(careers, current, seasons, lo, hi):
    """
    The orienting paragraph, so the page opens on what it covers rather
    than on a table.

    Deliberately thin: everything here is stated again, with working, in
    the sections below. It exists so someone arriving cold knows what they
    are looking at before they meet a percentile.
    """
    played = [car for car in careers if car.get('found')]
    if not played:
        return ''
    matchday = seasons[hi].matchday
    entries = [e for e in current if e['values'].get('Pts') is not None]

    cards = []

    def card(label, value, why):
        """
        One figure with its label and a line saying what it means.
        """
        cards.append('<div class="card"><div class="label">%s</div>'
                     '<div class="val">%s</div><div class="why">%s</div></div>'
                     % (esc(label), esc(value), esc(why)))

    card('Covering', 'LL%d - LL%d' % (lo, hi),
         '%d seasons of league-wide exports' % len(seasons))
    card('Roster', '%d players' % len(played),
         '%d of them in the current season' % len(entries))

    if entries:
        top = max(entries, key=lambda e: e['values']['Pts'])
        card('Leading the roster', top['username'],
             '%.0f pts, %s, %d of %d in rundle'
             % (top['values']['Pts'], top['rundle'], top['rank'],
                len(top.get('rundle_field') or [])) if top['rank']
             else '%.0f pts in %s' % (top['values']['Pts'], top['rundle']))

        rated = [e for e in entries if e['pcts'].get('Pts') is not None]
        if rated:
            rated.sort(key=lambda e: e['pcts']['Pts'])
            mid = rated[len(rated) // 2]
            card('Typical season so far',
                 '%s percentile' % ordinal(round(mid['pcts']['Pts'])),
                 'median roster member against their own level')

        forfeits = sum(e['values'].get('FL') or 0 for e in entries)
        if forfeits:
            card('Points lost to forfeits', '%d' % forfeits,
                 'across the roster this season, on top of the matches')

    lead = ('<p class="lead">This tracks %d LearnedLeague players across '
            'LL%d to LL%d, currently through match day %d of LL%d. Because '
            'the roster is spread over rundles A to R, raw totals are not '
            'comparable between them; almost everything below is therefore a '
            'percentile against every player at the same level in the same '
            'season, which is what makes a D season and an A season readable '
            'side by side.</p>'
            % (len(played), lo, hi, matchday, hi))

    return section(
        'summary', 'Overview',
        'What this covers and how to read it.',
        lead + '<div class="cards">%s</div>' % ''.join(cards))


def render_ratings(current, season):
    """
    Per-metric percentiles for the season on show.
    """
    if not current:
        return ''
    head = ['<tr><th>Player</th><th>Lvl</th>']
    for _, label, _, _ in METRICS:
        head.append('<th>%s</th>' % esc(label))
    head.append('<th>Cohort</th></tr>')
    body = []
    for ent in current:
        cells = ['<tr><td class="name">%s</td><td>%s</td>'
                 % (esc(ent['username']), level_pill(ent['level']))]
        for field, _, _, _ in METRICS:
            cells.append(bar_cell(ent['pcts'].get(field)))
        cells.append('<td class="dim">%d</td></tr>' % ent['cohort'])
        body.append(''.join(cells))
    return section(
        'ratings', 'How each number rates, LL%d' % season,
        'Every cell is a percentile against the same level that season. '
        'Cohort is how many players the comparison is drawn from.',
        '<div class="scroll"><table class="sortable"><thead>%s</thead>'
        '<tbody>%s</tbody></table></div>' % (''.join(head), '\n'.join(body)))


def render_moves(moves, prev_season, this_season):
    """
    Season over season movement as a diverging bar chart plus a table.
    """
    if not moves:
        return ''
    span = max(abs(mve['d_pts']) for mve in moves) or 1
    row_h, width, gutter, mid, reach = 22, 760, 132, 400, 226
    bars, rows = [], []
    for idx, mve in enumerate(moves):
        ypos = idx * row_h + 13
        length = (mve['d_pts'] / span) * reach
        xpos = mid if length >= 0 else mid + length
        colour = 'var(--accent)' if mve['d_pts'] >= 0 else 'var(--warm)'
        bars.append('<text x="%d" y="%d" text-anchor="end" font-size="11.5" '
                    'fill="var(--ink)">%s</text>'
                    % (gutter, ypos + 4, esc(mve['username'])))
        bars.append('<rect x="%.1f" y="%d" width="%.1f" height="12" rx="2" '
                    'fill="%s" fill-opacity=".85"/>'
                    % (xpos, ypos - 6, abs(length), colour))
        lab_x = mid + abs(length) + 7 if length >= 0 else xpos - 7
        bars.append('<text x="%.1f" y="%d" text-anchor="%s" font-size="10.5" '
                    'fill="var(--muted)">%+.0f</text>'
                    % (lab_x, ypos + 4, 'start' if length >= 0 else 'end',
                       mve['d_pts']))
        if mve['level_move'] > 0:
            move_txt = '<span class="up">%s &rarr; %s</span>' % (
                esc(mve['before']['level']), esc(mve['after']['level']))
        elif mve['level_move'] < 0:
            move_txt = '<span class="down">%s &rarr; %s</span>' % (
                esc(mve['before']['level']), esc(mve['after']['level']))
        else:
            move_txt = '<span class="dim">held %s</span>' % esc(
                mve['after']['level'])
        rows.append(
            '<tr><td class="name">%s</td><td class="dim">%s</td>'
            '<td class="dim">%s</td><td>%s</td><td>%s</td>%s%s<td>%s</td></tr>'
            % (esc(mve['username']), esc(mve['before']['rundle']),
               esc(mve['after']['rundle']),
               fmt(mve['before']['values'].get('Pts'), 0),
               fmt(mve['after']['values'].get('Pts'), 0),
               signed(mve['d_pts']), signed(mve['d_qpct'], 1), move_txt))
    height = len(moves) * row_h + 16
    chart = ('<svg viewBox="0 0 %d %d"><line x1="%d" y1="3" x2="%d" y2="%d" '
             'stroke="var(--line)"/>%s</svg>'
             % (width, height, mid, mid, height - 5, '\n'.join(bars)))
    head = ('<tr><th>Player</th><th>LL%d rundle</th><th>LL%d rundle</th>'
            '<th>LL%d pts</th><th>LL%d pts</th><th>&Delta; pts</th>'
            '<th>&Delta; QPct pp</th><th>Level</th></tr>'
            % (prev_season, this_season, prev_season, this_season))
    return section(
        'moves', 'LL%d to LL%d' % (prev_season, this_season),
        'Standings points move with rundle strength as much as with form, so '
        'read the level column alongside the change.',
        '%s<div class="scroll"><table class="sortable"><thead>%s</thead>'
        '<tbody>%s</tbody></table></div>' % (chart, head, '\n'.join(rows)))


def render_categories(export, roster_names):
    """
    The category plate: percentage correct by subject for everyone with a
    question history, shaded against the group average.
    """
    if not export:
        return ''
    cats = export.get('catList') or []
    names = [n for n in export.get('names', []) if n in roster_names]
    if not names or not cats:
        return ''
    table = {}
    for name in names:
        tallies = export['cats'].get(name) or []
        row = {}
        for i, cat in enumerate(cats):
            if i < len(tallies):
                ok, tot = tallies[i]
                if tot:
                    row[cat] = 100.0 * ok / tot
        if row:
            table[name] = row
    if len(table) < 2:
        return ''
    averages = {}
    for cat in cats:
        vals = [row[cat] for row in table.values() if cat in row]
        if vals:
            averages[cat] = statistics.mean(vals)
    order = sorted((c for c in cats if c in averages),
                   key=lambda c: -averages[c])
    head = ['<tr><th>Player</th>']
    for cat in order:
        head.append('<th style="font-size:9.5px">%s</th>' % esc(cat))
    head.append('<th>Mean</th></tr>')
    body = []
    for name in sorted(table, key=lambda n: -statistics.mean(
            list(table[n].values()))):
        row = table[name]
        cells = ['<tr><td class="name">%s</td>' % esc(name)]
        for cat in order:
            val = row.get(cat)
            if val is None:
                cells.append('<td class="dim">--</td>')
                continue
            delta = val - averages[cat]
            shade = max(0.0, min(1.0, abs(delta) / 22.0))
            colour = 'var(--accent)' if delta >= 0 else 'var(--warm)'
            cells.append('<td data-sort="%.2f" title="%+.1f vs group" '
                         'style="background:color-mix(in srgb,%s %d%%,'
                         'transparent)">%.0f</td>'
                         % (val, delta, colour, int(shade * 58), val))
        cells.append('<td>%.0f</td></tr>'
                     % statistics.mean(list(row.values())))
        body.append(''.join(cells))
    avg_row = ['<tr><td class="name dim">Group average</td>']
    for cat in order:
        avg_row.append('<td class="dim">%.0f</td>' % averages[cat])
    avg_row.append('<td class="dim">%.0f</td></tr>'
                   % statistics.mean(list(averages.values())))
    body.append(''.join(avg_row))
    return section(
        'categories', 'Category plate',
        'Percentage correct by subject, across the seasons Learned League '
        'keeps question history for. Green is ahead of this group\'s average '
        'for that column, orange behind; hover a cell for the gap. Columns '
        'run from the group\'s best subject to its worst.',
        '<div class="scroll"><table class="sortable"><thead>%s</thead>'
        '<tbody>%s</tbody></table></div>' % (''.join(head), '\n'.join(body)))


#
# Zero means no sample-size filter at all: everyone with any question
# history appears, newest players included.  Raise it with --min-questions
# if the thinnest rows are too noisy to be worth showing.
#
MIN_RANK_QUESTIONS = 0

#
# Diverging blue-to-red ramp for the rank plate, matching the palette used
# by the PNG companion so a shared image and the page agree.  Control
# points are the ends and quarters of a red/blue diverging scale; anything
# between them is interpolated.  Red marks a player's most distinctive
# subjects and blue their least -- within that one person's own profile,
# never against anybody else.
#
RANK_RAMP = [
    (0.00, (5, 48, 97)),
    (0.25, (67, 147, 195)),
    (0.50, (240, 240, 240)),
    (0.75, (214, 96, 77)),
    (1.00, (103, 0, 31)),
]


def ramp_colour(pos):
    """
    Interpolate the diverging ramp at pos, 0 to 1, returning (r, g, b).
    """
    pos = max(0.0, min(1.0, pos))
    for (lo_pos, lo_rgb), (hi_pos, hi_rgb) in zip(RANK_RAMP, RANK_RAMP[1:]):
        if pos <= hi_pos:
            span = hi_pos - lo_pos
            frac = 0.0 if span == 0 else (pos - lo_pos) / span
            return tuple(int(round(lo_rgb[i] + (hi_rgb[i] - lo_rgb[i]) * frac))
                         for i in range(3))
    return RANK_RAMP[-1][1]


def ramp_ink(pos):
    """
    Text colour that stays readable on the ramp: white on the dark ends,
    near-black through the pale middle.
    """
    return '#ffffff' if abs(pos - 0.5) > 0.32 else '#15181d'


def category_matrix(export, roster_names, minimum=0):
    """
    Per-category scores in log-odds.

    Percentages are worked in log-odds rather than raw, because raw
    percentages have a ceiling: for a player who scores eighty per cent
    everywhere, an easy category simply has no room left to show a
    strength, so their specialisms get pushed into hard categories by
    arithmetic alone.  On this roster that artefact shows up as a -0.50
    correlation between a player's overall level and the difficulty of
    their top-ranked category; in log-odds it falls to -0.14.

    The half-count added top and bottom keeps a clean sweep or a blank from
    running off to infinity.

    A category a player has never been asked about is left out of their row
    rather than scored, so rows can be ragged.  minimum drops players below
    a question count; at the default of zero nobody is dropped for sample
    size, which is what the newest players want even though their ordering
    is the loosest.

    Returns (categories, table, skipped) with table[name][category] a
    log-odds score.
    """
    cats = export.get('catList') or []
    table = {}
    thin = []
    for name in export.get('names', []):
        if name not in roster_names:
            continue
        tallies = export['cats'].get(name) or []
        total = sum(tot for _, tot in tallies)
        if minimum and total < minimum:
            thin.append(name)
            continue
        row = {cats[i]: math.log((ok + 0.5) / (tot - ok + 0.5))
               for i, (ok, tot) in enumerate(tallies)
               if i < len(cats) and tot > 0}
        if row:
            table[name] = row
    return cats, table, thin


def render_ranks(export, roster_names, minimum=MIN_RANK_QUESTIONS):
    """
    The shareable plate: everyone's categories ranked against themselves.

    Raw percentages invite a league table, which is not what this is for.
    So each score is double-centred first -- the player's own overall level
    is taken out, and so is the category's general difficulty -- by
    subtracting the row mean and the column mean and adding the grand mean
    back.  What is left is specialism: how a subject sits relative to the
    rest of that person's own profile.

    The scores come in as log-odds so that the arithmetic ceiling on a
    percentage does not decide where a strong player's specialisms appear.

    Those residuals are then ranked within each player, so every row runs 1
    to N regardless of how strong the player is.  Nobody's row is uniformly
    pale, no one is ranked against anyone else, and the shading uses a
    single hue so a high number reads as "less of a specialism for you"
    rather than as a failure.
    """
    if not export:
        return ''
    cats, table, thin = category_matrix(export, roster_names, minimum)
    if len(table) < 3 or not cats:
        return ''
    names = sorted(table, key=str.lower)
    order = sorted(cats)
    #
    # Rows can be ragged: a player who has never been asked a World History
    # question has no World History score.  Every mean is therefore taken
    # over what is actually present, and a player is ranked across their
    # own categories only.
    #
    col_mean = {}
    for cat in order:
        vals = [table[n][cat] for n in names if cat in table[n]]
        if vals:
            col_mean[cat] = statistics.mean(vals)
    row_mean = {n: statistics.mean(list(table[n].values())) for n in names}
    grand = statistics.mean(list(col_mean.values()))
    ranks, best, spans = {}, {}, {}
    for name in names:
        resid = [(c, table[name][c] - row_mean[name] - col_mean[c] + grand)
                 for c in order if c in table[name] and c in col_mean]
        resid.sort(key=lambda cr: -cr[1])
        ranks[name] = {c: i + 1 for i, (c, _) in enumerate(resid)}
        best[name] = [c for c, _ in resid[:3]]
        spans[name] = len(resid)
    top = len(order)
    head = ['<tr><th>Player</th>']
    for cat in order:
        head.append('<th style="font-size:9.5px">%s</th>' % esc(cat))
    head.append('<th>Their top three</th></tr>')
    body = []
    for name in names:
        cells = ['<tr><td class="name">%s</td>' % esc(name)]
        for cat in order:
            place = ranks[name].get(cat)
            if place is None:
                cells.append('<td class="dim" data-sort="99" '
                             'title="%s: never asked">&middot;</td>'
                             % esc(cat))
                continue
            #
            # Rank one sits at the red end, the last rank at the blue end,
            # scaled across this row only.
            #
            span = spans[name]
            pos = (span - place) / max(1, span - 1)
            rgb = ramp_colour(pos)
            cells.append('<td data-sort="%d" title="%s: rank %d of %d for %s" '
                         'style="background:rgb(%d,%d,%d);color:%s;'
                         'font-weight:600">%d</td>'
                         % (place, esc(cat), place, span, esc(name),
                            rgb[0], rgb[1], rgb[2], ramp_ink(pos), place))
        cells.append('<td class="dim" style="text-align:left">%s</td></tr>'
                     % esc(', '.join(best[name])))
        body.append(''.join(cells))
    note = (
        'Every row is one person ranked against nothing but themselves: 1 is '
        'the subject they are most distinctively good at, %d the one they '
        'lean on least. Before ranking, each score has the player\'s own '
        'overall level taken out and the category\'s general difficulty '
        'taken out, so an easy category does not flatter everyone and a '
        'strong player does not sweep the board. Nobody is compared with '
        'anybody else here. A dot means that player has never been asked '
        'anything in that category, so it is left unranked rather than '
        'guessed at. '
        'Rows are alphabetical on purpose. Red marks a person\'s most '
        'distinctive subjects and blue their least, always within their own '
        'profile.' % top)
    if thin:
        note += (' Left out for thin samples, under %d questions answered: '
                 '%s.' % (minimum, esc(', '.join(sorted(thin)))))
    note += (' A player who has answered only a few dozen questions has '
             'much noisier ranks than one with thousands; the order is '
             'still theirs, just held more loosely.')
    return section(
        'ranks', 'Category ranks, everyone against themselves', note,
        '<div class="scroll"><table class="sortable"><thead>%s</thead>'
        '<tbody>%s</tbody></table></div>' % (''.join(head), '\n'.join(body)))


def render_hun(export, roster_names):
    """
    The HUN plate: how alike any two players answer.
    """
    if not export:
        return ''
    names = export.get('names') or []
    keep = [i for i, n in enumerate(names) if n in roster_names]
    if len(keep) < 2:
        return ''
    grid, overlap = {}, {}
    for i, j, val, tot in export.get('pairs', []):
        if val is None:
            continue
        grid[(i, j)] = grid[(j, i)] = val
        overlap[(i, j)] = overlap[(j, i)] = tot
    vals = [v for v in grid.values()]
    if not vals:
        return ''
    low, high = min(vals), max(vals)
    spread = (high - low) or 1.0
    head = ['<tr><th>HUN</th>']
    for i in keep:
        head.append('<th style="font-size:9.5px">%s</th>' % esc(names[i]))
    head.append('</tr>')
    body = []
    for i in keep:
        cells = ['<tr><td class="name">%s</td>' % esc(names[i])]
        for j in keep:
            if i == j:
                cells.append('<td class="dim">&mdash;</td>')
                continue
            val = grid.get((i, j))
            if val is None:
                cells.append('<td class="dim">--</td>')
                continue
            tot = overlap.get((i, j), 0)
            if tot < 100:
                cells.append('<td class="dim" data-sort="%.4f" '
                             'title="only %d shared questions">%.3f</td>'
                             % (val, tot, val))
                continue
            shade = (val - low) / spread
            cells.append('<td data-sort="%.4f" title="%d shared questions" '
                         'style="background:color-mix(in srgb,var(--accent) '
                         '%d%%,transparent)">%.3f</td>'
                         % (val, tot, int(shade * 62), val))
        cells.append('</tr>')
        body.append(''.join(cells))
    solid = [(k, v) for k, v in grid.items()
             if k[0] < k[1] and overlap.get(k, 0) >= 300]
    note = ''
    if solid:
        top = max(solid, key=lambda kv: kv[1])
        bot = min(solid, key=lambda kv: kv[1])
        note = (' Closest pair on a solid sample: <strong>%s</strong> and '
                '<strong>%s</strong> at %.3f. Least alike: <strong>%s</strong> '
                'and <strong>%s</strong> at %.3f.'
                % (esc(names[top[0][0]]), esc(names[top[0][1]]), top[1],
                   esc(names[bot[0][0]]), esc(names[bot[0][1]]), bot[1]))
    return section(
        'hun', 'HUN plate',
        'The share of shared questions on which two players got the same '
        'result -- right together or wrong together. Pairs with fewer than '
        '100 questions in common are greyed out as too thin to trust.' + note,
        '<div class="scroll"><table class="sortable"><thead>%s</thead>'
        '<tbody>%s</tbody></table></div>' % (''.join(head), '\n'.join(body)))


def jacobi_eigen(mat, sweeps=80):
    """
    Eigenvalues and eigenvectors of a small symmetric matrix, by cyclic
    Jacobi rotation.

    The covariance matrices here are at most eighteen square, so the simple
    quadratic method is instant and saves the package a numpy dependency it
    has never had.  Returns (values, vectors) with vectors held by column.
    """
    size = len(mat)
    work = [[float(x) for x in row] for row in mat]
    vecs = [[1.0 if i == j else 0.0 for j in range(size)]
            for i in range(size)]
    for _ in range(sweeps):
        off = math.sqrt(sum(work[i][j] ** 2 for i in range(size)
                            for j in range(size) if i != j))
        if off < 1e-11:
            break
        for p in range(size - 1):
            for q in range(p + 1, size):
                if abs(work[p][q]) < 1e-13:
                    continue
                theta = (work[q][q] - work[p][p]) / (2.0 * work[p][q])
                sign = 1.0 if theta >= 0 else -1.0
                tan = sign / (abs(theta) + math.sqrt(theta * theta + 1.0))
                cos = 1.0 / math.sqrt(tan * tan + 1.0)
                sin = tan * cos
                for k in range(size):
                    akp, akq = work[k][p], work[k][q]
                    work[k][p] = cos * akp - sin * akq
                    work[k][q] = sin * akp + cos * akq
                for k in range(size):
                    apk, aqk = work[p][k], work[q][k]
                    work[p][k] = cos * apk - sin * aqk
                    work[q][k] = sin * apk + cos * aqk
                for k in range(size):
                    vkp, vkq = vecs[k][p], vecs[k][q]
                    vecs[k][p] = cos * vkp - sin * vkq
                    vecs[k][q] = sin * vkp + cos * vkq
    return [work[i][i] for i in range(size)], vecs


def pca_2d(matrix):
    """
    Standardised two-component PCA.

    Columns are z-scored first, so a metric measured in points does not
    outrank one measured in percent purely by having bigger numbers.
    Returns (scores, loadings, explained) where explained is the percentage
    of total variance carried by each of the first two components.
    """
    rows, cols = len(matrix), len(matrix[0])
    means = [sum(r[j] for r in matrix) / rows for j in range(cols)]
    sds = []
    for j in range(cols):
        var = sum((r[j] - means[j]) ** 2 for r in matrix) / rows
        sds.append(math.sqrt(var))
    data = [[(r[j] - means[j]) / sds[j] for j in range(cols)]
            for r in matrix]
    cov = [[sum(data[k][i] * data[k][j] for k in range(rows)) / (rows - 1)
            for j in range(cols)] for i in range(cols)]
    vals, vecs = jacobi_eigen(cov)
    order = sorted(range(cols), key=lambda i: -vals[i])
    total = sum(v for v in vals if v > 0) or 1.0
    first, second = order[0], order[1]
    scores = [[sum(row[j] * vecs[j][axis] for j in range(cols))
               for axis in (first, second)] for row in data]
    loads = [[vecs[j][axis] for axis in (first, second)]
             for j in range(cols)]
    explained = [max(0.0, vals[axis]) / total * 100.0
                 for axis in (first, second)]
    return scores, loads, explained


def render_biplot(labels, matrix, features, explained_note, width=None,
                  height=None, max_labels=None):
    """
    Draw one PCA biplot: a point per player, an arrow per input measure.

    An arrow points the way that measure increases; players lying out along
    it score high on it.  Arrows close together describe measures that move
    together across this group.

    Every arrow is drawn, but max_labels caps how many are named.  With
    eighteen categories, most of which load the same way on the first
    component, naming them all produces a thicket rather than a chart, so
    only the longest arrows get a label.
    """
    scores, loads, explained = pca_2d(matrix)
    #
    # Give the canvas room in proportion to how many names have to fit on
    # it; a crowded plot runs the label placer out of free slots and the
    # names start overlapping.
    #
    if width is None:
        width = max(860, 700 + 7 * len(labels))
    if height is None:
        height = max(620, 470 + 6 * len(labels))
    pad = 54
    inner_w, inner_h = width - pad * 2, height - pad - 46
    xs = [s[0] for s in scores]
    ys = [s[1] for s in scores]
    span_x = max(max(xs) - min(xs), 1e-6)
    span_y = max(max(ys) - min(ys), 1e-6)
    mid_x, mid_y = (max(xs) + min(xs)) / 2.0, (max(ys) + min(ys)) / 2.0
    scale = min(inner_w / span_x, inner_h / span_y) * 0.86

    def place(sx, sy):
        """
        Component scores to svg coordinates.
        """
        return (pad + inner_w / 2.0 + (sx - mid_x) * scale,
                pad + inner_h / 2.0 - (sy - mid_y) * scale)

    parts = ['<svg viewBox="0 0 %d %d" role="img" aria-label="PCA biplot">'
             % (width, height)]
    zero = place(mid_x, mid_y)
    parts.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" '
                 'stroke="var(--grid)"/>'
                 % (pad, zero[1], pad + inner_w, zero[1]))
    parts.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" '
                 'stroke="var(--grid)"/>'
                 % (zero[0], pad - 12, zero[0], pad + inner_h))
    arrow_scale = min(inner_w, inner_h) * 0.42
    placed = []
    #
    # Loadings often bunch together -- in a category analysis nearly every
    # subject loads the same way on the first component -- so the arrow
    # labels go through the same collision avoidance as the point labels,
    # strongest loading first so the ones that matter get the best slots.
    #
    arrows = sorted(zip(features, loads),
                    key=lambda fl: -(fl[1][0] ** 2 + fl[1][1] ** 2))
    cap = len(arrows) if max_labels is None else max_labels
    for rank, (name, load) in enumerate(arrows):
        tip_x = zero[0] + load[0] * arrow_scale
        tip_y = zero[1] - load[1] * arrow_scale
        parts.append('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" '
                     'stroke="var(--warm)" stroke-width="1" opacity="%s">'
                     '<title>%s</title></line>'
                     % (zero[0], zero[1], tip_x, tip_y,
                        '.55' if rank < cap else '.3', esc(name)))
        if rank >= cap:
            continue
        text_x, text_y, anchor = place_label(name, tip_x, tip_y,
                                             pad + inner_w, placed)
        parts.append('<text x="%.1f" y="%.1f" text-anchor="%s" font-size="9.5"'
                     ' fill="var(--warm)" opacity=".95">%s</text>'
                     % (text_x, text_y, anchor, esc(name)))
    for label, score in zip(labels, scores):
        xpos, ypos = place(score[0], score[1])
        parts.append('<circle cx="%.1f" cy="%.1f" r="5" fill="var(--accent)" '
                     'fill-opacity=".85"/>' % (xpos, ypos))
        tx, ty, anchor = place_label(label, xpos, ypos, pad + inner_w, placed)
        parts.append('<text x="%.1f" y="%.1f" text-anchor="%s" font-size="11" '
                     'fill="var(--ink)">%s</text>'
                     % (tx, ty, anchor, esc(label)))
    parts.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="11.5" '
                 'fill="var(--muted)">PC1 &mdash; %.0f%% of variance</text>'
                 % (pad + inner_w / 2.0, height - 8, explained[0]))
    parts.append('<text transform="translate(14,%.1f) rotate(-90)" '
                 'text-anchor="middle" font-size="11.5" fill="var(--muted)">'
                 'PC2 &mdash; %.0f%% of variance</text>'
                 % (pad + inner_h / 2.0, explained[1]))
    parts.append('</svg>')
    return ('<p class="note" style="margin:14px 0 4px"><strong>%s</strong> '
            'PC1 and PC2 together carry %.0f%% of the variation.</p>%s'
            % (explained_note, explained[0] + explained[1], ''.join(parts)))


MIN_PCA_QUESTIONS = 0

#
# The performance analysis needs a spread, and a spread needs at least two
# seasons to be a number at all.  Two is therefore the floor: below it a
# player cannot be placed, which is a structural limit rather than a view
# about whether they have played enough.
#
MIN_PCA_SEASONS = 2


def render_pca(export, roster_names, careers, minimum=MIN_PCA_QUESTIONS,
               min_seasons=MIN_PCA_SEASONS):
    """
    Two biplots: one on what players know, one on how they play.
    """
    blocks = []

    if export:
        cats = export.get('catList') or []
        labels, matrix, thin, gaps = [], [], [], []
        for name in export.get('names', []):
            if name not in roster_names:
                continue
            tallies = export['cats'].get(name) or []
            total = sum(tot for _, tot in tallies)
            row = [100.0 * ok / tot if tot else None
                   for ok, tot in tallies]
            if minimum and total < minimum:
                thin.append(name)
                continue
            #
            # A component analysis needs a value in every column; there is
            # nothing sensible to put where a player has never been asked a
            # question, and inventing one would move the components.  So
            # this exclusion is structural, not a judgement about sample
            # size.
            #
            if any(v is None for v in row):
                gaps.append(name)
                continue
            labels.append(name)
            matrix.append(row)
        if len(labels) >= 4:
            note = ('<em>What they know.</em> Each player is their profile '
                    'across the %d question categories, standardised so a '
                    'wide-ranging category does not dominate a narrow one. '
                    'Players sitting near each other have similar subject '
                    'strengths, which is the same question the HUN plate '
                    'asks from the other direction.' % len(cats))
            if thin:
                note += (' Left out for thin samples, under %d questions '
                         'answered: %s.'
                         % (minimum, esc(', '.join(sorted(thin)))))
            if gaps:
                note += (' Left out because a component analysis needs every '
                         'column filled and these players have a category '
                         'they have never been asked about: %s.'
                         % esc(', '.join(sorted(gaps))))
            note += (' Every category is drawn, but only the eight longest '
                     'arrows are named to keep the chart readable; hover any '
                     'arrow for its category.')
            blocks.append(render_biplot(labels, matrix, cats, note,
                                        max_labels=8))

    feats = ['Pts pct', 'QPct pct', 'TCA pct', 'MPD pct', 'OE pct', 'DE pct',
             'Conversion', 'Spread']
    labels, matrix, short = [], [], []
    for car in careers:
        if not car.get('found'):
            continue
        if car['count'] < max(2, min_seasons):
            short.append(car['username'])
            continue
        vals = []
        for field in ('Pts', 'QPct', 'TCA', 'MPD', 'OE', 'DE'):
            got = [ent['pcts'].get(field) for ent in car['seasons']
                   if ent['pcts'].get(field) is not None]
            vals.append(statistics.mean(got) if got else None)
        convs = [ent['conversion'] for ent in car['seasons']
                 if ent['conversion'] is not None]
        vals.append(statistics.mean(convs) if convs else None)
        vals.append(car['spread'])
        if any(v is None for v in vals):
            continue
        labels.append(car['username'])
        matrix.append(vals)
    if len(labels) >= 4:
        play_note = (
            '<em>How they play.</em> Career averages of each level '
            'percentile, plus conversion and spread. This one separates '
            'players by style rather than by subject: offence against '
            'defence, and steadiness against streakiness.')
        if short:
            play_note += (
                ' Spread is how much a player varies from season to season, '
                'so it takes at least %d seasons to exist at all. These '
                'players have fewer and cannot be placed here yet: %s.'
                % (max(2, min_seasons), esc(', '.join(sorted(short)))))
        blocks.append(render_biplot(labels, matrix, feats, play_note))
    if not blocks:
        return ''
    return section(
        'pca', 'Principal components',
        'Two views of the same roster, each squeezing many correlated '
        'measures into the two directions that carry the most variation. '
        'Orange arrows are the input measures: a player lying out along an '
        'arrow scores high on it, and arrows pointing the same way are '
        'measures that rise and fall together across this group. Distances '
        'between players are what matter, not the axis numbers.',
        ''.join(blocks))


def render_about(seasons, careers, export, lo, hi):
    """
    Provenance and glossary, so no number is a mystery.
    """
    files = []
    for season in sorted(seasons):
        data = seasons[season]
        files.append('<li><code>%s</code> &mdash; LL%d through match day %d, '
                     '%d players</li>'
                     % (esc(os.path.basename(data.path)), season,
                        data.matchday, data.total))
    if export:
        files.append('<li><code>%s</code> &mdash; question histories and HUN '
                     'pairs for %d players</li>'
                     % (esc(os.path.basename(export.get('_path', ''))),
                        len(export.get('names', []))))
    absent = [car['username'] for car in careers if not car.get('found')]
    miss = ''
    if absent:
        miss = ('<p class="note">On the roster but in none of these exports: '
                '%s.</p>' % esc(', '.join(sorted(absent))))
    gloss = ''.join('<dt>%s</dt><dd>%s</dd>' % (esc(t), esc(d))
                    for t, d in GLOSSARY)
    return section(
        'about', 'Where this came from',
        'Seasons LL%d to LL%d. %d of %d rostered players appear.'
        % (lo, hi, sum(1 for c in careers if c.get('found')), len(careers)),
        '<ul class="note">%s</ul>%s<h2 style="margin-top:16px">Glossary</h2>'
        '<dl class="gloss">%s</dl><footer>Percentiles, conversion, spread, '
        'swing and the awards are computed by this script from the league '
        'exports. W-L-T, Pts, TCA, QPct, MPD, OE and DE are carried through '
        'unchanged. Level comes from the export where present, otherwise '
        'from the rundle name.</footer>'
        % (''.join(files), miss, gloss))


def render(seasons, careers, export, args):
    """
    Assemble the page and write it out.
    """
    lo, hi = min(seasons), max(seasons)
    prev = sorted(seasons)[-2] if len(seasons) > 1 else None
    current = current_entries(careers, hi)

    #
    # User name to group, for the group picker in the toolbar.
    #
    groups_js = 'var LL_GROUPS = %s;\n' % json.dumps(
        {car['username']: car['group'] for car in careers}, sort_keys=True)
    group_opts = ['<option value="">all groups</option>']
    group_opts += ['<option value="%s">%s</option>' % (esc(name), esc(name))
                   for name in group_order(careers)]

    #
    # Every player-season, for the correlation explorer. Parallel arrays
    # keyed by LL_FIELDS rather than objects, since this block scales with
    # players times seasons times measures.
    #
    ex_fields, ex_rows = explorer_payload(careers)
    groups_js += 'var LL_FIELDS = %s;\n' % json.dumps(ex_fields)
    groups_js += 'var LL_ROWS = %s;\n' % json.dumps(ex_rows)
    moves = season_moves(careers, prev, hi) if prev else []
    roster_names = {car['username'] for car in careers}
    order = group_order([{'group': car['group']} for car in careers])
    wanted = args.sections
    blocks = {
        'awards': lambda: render_awards(
            superlatives(careers, current, moves)),
        'groups': lambda: render_groups(careers, current, order),
        'standings': lambda: render_standings(current, hi,
                                              seasons[hi].matchday),
        'careers': lambda: render_careers(careers, lo, hi),
        'levels': lambda: render_levels(careers, lo, hi),
        'scatter': lambda: render_scatter(current, hi),
        'ratings': lambda: render_ratings(current, hi),
        'outlook': lambda: render_outlook(current, hi, seasons[hi].matchday),
        'profile': lambda: render_profile(current, hi),
        'summary': lambda: render_summary(careers, current, seasons,
                                          lo, hi),
        'explorer': lambda: render_explorer(careers),
        'record': lambda: render_record(current, hi),
        'moves': lambda: render_moves(moves, prev, hi) if moves else '',
        'categories': lambda: render_categories(export, roster_names),
        'referrals': lambda: render_referrals(careers),
        'ranks': lambda: render_ranks(export, roster_names, args.minimum),
        'hun': lambda: render_hun(export, roster_names),
        'pca': lambda: render_pca(export, roster_names, careers,
                                  args.minimum, args.min_seasons),
        'about': lambda: render_about(seasons, careers, export, lo, hi),
    }
    body, toc = [], []
    titles = {'summary': 'Overview', 'standings': 'Season standings',
              'outlook': 'Position in rundle',
              'ratings': 'Percentile ratings',
              'profile': 'Offence and defence',
              'record': 'Record against match points',
              'scatter': 'Knowledge against results',
              'moves': 'Season-over-season change',
              'careers': 'Career records', 'levels': 'Rundle history',
              'explorer': 'Correlation explorer',
              'categories': 'Category accuracy', 'ranks': 'Category ranks',
              'hun': 'Answer similarity', 'pca': 'Principal components',
              'groups': 'By group', 'referrals': 'Referral tree',
              'awards': 'Superlatives', 'about': 'Method and glossary'}
    for name in ALL_SECTIONS:
        if name not in wanted:
            continue
        chunk = blocks[name]()
        if chunk:
            body.append(chunk)
            toc.append('<a href="#%s">%s</a>' % (name, esc(titles[name])))
    played = sum(1 for car in careers if car.get('found'))
    page = (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>%s</title><style>%s</style></head><body><div class="wrap">'
        '<header><div><h1>%s</h1><p class="sub">LL%d&ndash;LL%d &middot; '
        '%d players &middot; current season LL%d through match day %d '
        '&middot; updated %s</p></div><div class="tools">'
        '<input id="filter" type="search" placeholder="filter players">'
        '<select id="group">%s</select>'
        '<button id="theme" type="button">theme: auto</button></div></header>'
        '<nav class="toc">%s</nav>%s</div><script>%s</script></body></html>'
        % (esc(args.title), CSS, esc(args.title), lo, hi, played, hi,
           seasons[hi].matchday, args.as_of,
           ''.join(group_opts), ''.join(toc), ''.join(body),
           groups_js + PAGE_JS))
    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    with open(args.out, 'w', encoding='utf-8') as fdesc:
        fdesc.write(page)
    return args.out


def find_players(text, seasons):
    """
    Search the newest export for player names containing text, printing the
    matches as roster-ready rows.
    """
    needle = norm_name(text)
    season = max(seasons)
    data = seasons[season]
    hits = []
    for row in read_csv_rows(data.path):
        if needle in norm_name(row['Player']):
            hits.append(row)
    if not hits:
        print("\nNo player in LL%d matches '%s'." % (season, text))
        return
    hits.sort(key=lambda row: row['Player'].lower())
    print('\n%d match(es) in LL%d:\n' % (len(hits), season))
    for row in hits[:40]:
        print('  %-20s %-24s %3s pts, rank %-3s (level %s)'
              % (row['Player'], row.get('Rundle', ''), row.get('Pts', ''),
                 row.get('Rundle Rank', ''), level_of(row)))
    print('\nRoster rows to paste in:\n')
    for row in hits[:40]:
        print('%s,' % row['Player'])


def main(argv=None):
    """
    Parse arguments, load what is on disk, build the page.
    """
    parser = argparse.ArgumentParser(description=DEFAULT_TITLE)
    parser.add_argument('--data', action='append', metavar='DIR',
                        help='directory to search for exports (repeatable)')
    parser.add_argument('--roster', default=DEFAULT_ROSTER,
                        help='roster csv (created on first run)')
    parser.add_argument('--out', default=DEFAULT_OUTPUT,
                        help='html file to write')
    parser.add_argument('--title', default=DEFAULT_TITLE,
                        help='heading and browser title for the page')
    parser.add_argument('--as-of', dest='as_of', metavar='YYYY-MM-DD',
                        default=datetime.date.today().isoformat(),
                        help='date shown in the subtitle (default today). CI '
                             'passes the data commit date so that rebuilding '
                             'unchanged data produces an identical page')
    parser.add_argument('--since', type=int, metavar='N',
                        help='ignore seasons before N')
    parser.add_argument('--until', type=int, metavar='N',
                        help='ignore seasons after N')
    parser.add_argument('--sections', default='all',
                        help='which sections to build: a preset (%s) or a '
                             'comma separated list of %s'
                             % ('/'.join(sorted(SECTION_SETS)),
                                ','.join(ALL_SECTIONS)))
    parser.add_argument('--open', action='store_true', dest='open_it',
                        help='open the page in a browser when done')
    parser.add_argument('--min-questions', type=int, default=0,
                        dest='minimum', metavar='N',
                        help='drop players with fewer than N questions '
                             'answered from the category rank plate and the '
                             'category PCA (default 0, meaning no filter)')
    parser.add_argument('--min-seasons', type=int, default=MIN_PCA_SEASONS,
                        dest='min_seasons', metavar='N',
                        help='seasons a player needs before appearing in the '
                             'performance PCA (default %d, the floor at '
                             'which a season-to-season spread exists)'
                             % MIN_PCA_SEASONS)
    parser.add_argument('--find', metavar='TEXT',
                        help='search the newest export for a name and exit')
    args = parser.parse_args(argv)

    if args.sections in SECTION_SETS:
        args.sections = SECTION_SETS[args.sections]
    else:
        args.sections = [s.strip() for s in args.sections.split(',')
                         if s.strip()]
        bad = [s for s in args.sections if s not in ALL_SECTIONS]
        if bad:
            parser.error('unknown section(s): %s' % ', '.join(bad))

    roots = args.data or DEFAULT_DATA_ROOTS
    roster = load_roster(args.roster)
    wanted = {norm_name(person['username']) for person in roster}
    print('Roster: %d players from %s' % (len(roster), args.roster))
    seasons = load_seasons(roots, wanted, args.since, args.until)
    if not seasons:
        parser.error('no *Leaguewide*.csv found under %s' % ', '.join(roots))
    print('Seasons: LL%d to LL%d (%d exports)'
          % (min(seasons), max(seasons), len(seasons)))

    if args.find:
        find_players(args.find, seasons)
        return 0

    export = load_qhist_export(roots)
    if export:
        print('Question history: %d players' % len(export.get('names', [])))
    careers = build_careers(roster, seasons)
    out = render(seasons, careers, export, args)
    print('Wrote %s' % os.path.abspath(out))
    if args.open_it:
        webbrowser.open('file://' + os.path.abspath(out))
    return 0


if __name__ == '__main__':
    sys.exit(main())
