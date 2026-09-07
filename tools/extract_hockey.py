#!/usr/bin/env python3
"""Turn the private fantasy hockey exports into anonymised data for the site.

The Yahoo exports carry manager first names, and the team files carry manager
email addresses. None of that belongs on a public page, so this reads the
exports locally and writes out a file keyed by team name only. The manager
column is used to join seasons together and is then dropped; it is never
written, which is why the published repo can hold the result.

Team names change from season to season, so each manager is given one stable
alias -- the name they use in the most recent season they appear in -- and
every season is relabelled to it. Without that, a career table cannot join a
person's rows together.

Usage:
    python tools/extract_hockey.py --source C:/Users/cdmac/bettman-cometh-fantasy-hockey-2026
    python tools/extract_hockey.py --source ... --check   # report, write nothing
"""
import argparse
import csv
import glob
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUTPUT = os.path.join(ROOT, 'HK', 'data', 'hockey.json')

# Seasons are identified by their Yahoo league key. The exports name their
# folders inconsistently -- the same league appears under both 2025 and 2026 --
# so the key is what actually distinguishes a season.
SEASON_LABEL = {
    '453.l.82957': '2024-25',
    '465.l.20819': '2025-26',
}


def read_csv(path):
    """
    Yahoo writes a BOM and the odd non-UTF-8 byte in team names.
    """
    with io.open(path, encoding='utf-8-sig', errors='replace', newline='') as fh:
        return list(csv.DictReader(fh))


def league_key(path):
    """
    Pull '465.l.20819' out of a filename or a team key.
    """
    m = re.search(r'(\d+)[._]l[._](\d+)', os.path.basename(path))
    return '%s.l.%s' % (m.group(1), m.group(2)) if m else None


def find(source, pattern):
    """
    Every match for a glob under the source tree, virtualenvs excluded.
    """
    hits = glob.glob(os.path.join(source, '**', pattern), recursive=True)
    return [h for h in hits
            if '.venv' not in h and 'site-packages' not in h]


def build_aliases(source):
    """
    manager -> the team name to publish them under, plus per-season lookups.

    The most recent season a manager appears in supplies the alias, so the
    page reads in today's names rather than whatever they called themselves
    three years ago.
    """
    by_season = {}
    ids = {}
    for path in find(source, '*-Team.csv'):
        key = league_key(path)
        if not key:
            continue
        rows = read_csv(path)
        pairs = {}
        for row in rows:
            manager = (row.get('Manager') or '').strip()
            name = (row.get('Name') or '').strip()
            team_id = (row.get('ID') or '').strip()
            if manager and name:
                pairs[manager] = name
            if manager and team_id:
                ids.setdefault(key, {})[team_id] = manager
        if pairs:
            by_season.setdefault(key, {}).update(pairs)

    # live_rosters is the freshest source and covers the running season.
    for path in find(source, 'live_rosters.csv'):
        for row in read_csv(path):
            manager = (row.get('manager_name') or '').strip()
            name = (row.get('team_name') or '').strip()
            key = league_key(row.get('team_key') or '')
            if manager and name and key:
                by_season.setdefault(key, {})[manager] = name
            team_id = (row.get('team_key') or '').strip()
            if manager and team_id and key:
                ids.setdefault(key, {})[team_id] = manager

    order = sorted(by_season, key=lambda k: SEASON_LABEL.get(k, k))
    alias = {}
    for key in order:                      # later seasons overwrite earlier
        alias.update(by_season[key])
    return alias, by_season, ids


def load_current(source, alias):
    """
    The running season, from the long-format weekly matchup file.

    Its Team_Name column holds manager names despite the header, which is
    exactly the sort of thing that would otherwise reach the page.
    """
    hits = find(source, 'matchup_long_*.csv')
    if not hits:
        return None
    rows = read_csv(sorted(hits)[-1])

    seen, matchups = set(), []
    for row in rows:
        key = league_key(row.get('Team_Key') or '')
        week = int(float(row['Week']))
        home = alias.get((row.get('Team_Name') or '').strip())
        away = alias.get((row.get('Opp_Name') or '').strip())
        if not home or not away:
            continue
        pair = (week, tuple(sorted([home, away])))
        if pair in seen:                   # long format lists each match twice
            continue
        seen.add(pair)
        matchups.append({'w': week, 'a': home,
                         'ap': round(float(row['Points']), 2),
                         'b': away,
                         'bp': round(float(row['Opp_Points']), 2)})

    teams = sorted({m['a'] for m in matchups} | {m['b'] for m in matchups})
    return {'label': SEASON_LABEL.get(key, key or 'current'),
            'weeks': max(m['w'] for m in matchups),
            'teams': teams, 'matchups': matchups}


def load_history(source, ids, alias, skip_key):
    """
    Earlier seasons, from the wide-format matchup exports.

    Joined on team id rather than on the team name. Names are not stable
    within a season either -- one team is 'Bad habits' in the team export and
    'Trade Warriors' throughout the matchups, having been renamed partway
    through -- and matching on the name silently dropped every one of its
    games along with the team itself.
    """
    seasons = []
    for path in sorted(find(source, '*-Matchup.csv')):
        key = league_key(path)
        if not key or key == skip_key:
            continue
        # That season's team id -> manager -> published alias.
        to_alias = {}
        for team_id, manager in (ids.get(key) or {}).items():
            if manager in alias:
                to_alias[team_id] = alias[manager]

        matchups = []
        for row in read_csv(path):
            if (row.get('Complete') or '').strip().lower() != 'true':
                continue
            home = to_alias.get((row.get('Team 1 ID') or '').strip())
            away = to_alias.get((row.get('Team 2 ID') or '').strip())
            if not home or not away:
                continue
            try:
                matchups.append({'w': int(float(row['Week'])), 'a': home,
                                 'ap': round(float(row['Team 1 Points']), 2),
                                 'b': away,
                                 'bp': round(float(row['Team 2 Points']), 2)})
            except (TypeError, ValueError):
                continue
        if not matchups:
            continue
        teams = sorted({m['a'] for m in matchups} | {m['b'] for m in matchups})
        seasons.append({'label': SEASON_LABEL.get(key, key),
                        'weeks': max(m['w'] for m in matchups),
                        'teams': teams, 'matchups': matchups})
    return seasons


def load_rosters(source, alias):
    """
    Who is on each roster, joined by team key rather than by manager.

    NHL players are public figures and are named; the fantasy managers who
    picked them are not.
    """
    key_to_team = {}
    for path in find(source, 'live_rosters.csv'):
        for row in read_csv(path):
            manager = (row.get('manager_name') or '').strip()
            if manager in alias:
                key_to_team[(row.get('team_key') or '').strip()] = alias[manager]
    if not key_to_team:
        return {}

    rosters = {}
    for path in find(source, 'nhl_skater_stats.csv') + \
            find(source, 'nhl_goalie_stats.csv'):
        for row in read_csv(path):
            team = key_to_team.get((row.get('yahoo_team_id') or '').strip())
            if not team:
                continue
            kind = 'goalies' if (row.get('type') == 'goalie') else 'skaters'
            entry = {'name': row.get('name'), 'pos': row.get('position'),
                     'nhl': row.get('nhl_team')}
            fields = (('GP', 'W', 'L', 'GAA', 'SV_PCT', 'SHO')
                      if kind == 'goalies'
                      else ('GP', 'G', 'A', 'PTS', 'PlusMinus', 'PIM', 'PPP',
                            'SOG'))
            for field in fields:
                try:
                    entry[field] = float(row[field])
                except (KeyError, TypeError, ValueError):
                    entry[field] = None
            rosters.setdefault(team, {'skaters': [], 'goalies': []})
            rosters[team][kind].append(entry)
    return rosters


def audit(payload, alias):
    """
    Refuse to write anything that identifies a manager.

    This is the whole point of the script, so it is checked rather than
    trusted. A blunt substring scan is no good here: several managers share a
    first name with an NHL player on someone's roster (Drew Doughty, Evan
    Bouchard, Trevor Zegras), and one team is called "Cameron with the good
    fur" while belonging to somebody else entirely. What matters is whether a
    manager name is used as an identity, so the check is on exact values in
    the places identity lives, plus a sweep for email addresses.
    """
    managers = {m for m in alias if m}
    published = set(alias.values())
    problems = []

    identities = set(payload['rosters'])
    for season in [payload['current']] + payload['history']:
        identities.update(season['teams'])
        for match in season['matchups']:
            identities.add(match['a'])
            identities.add(match['b'])

    for who in sorted(identities & managers):
        if who not in published:
            problems.append('used as a team identity: %s' % who)

    blob = json.dumps(payload)
    for hit in set(re.findall(r'[\w.+-]+@[\w-]+\.[\w.]+', blob)):
        problems.append('email address: %s' % hit)
    for key in ('manager', 'manager_name', 'manager_email', 'email'):
        if '"%s"' % key in blob:
            problems.append('field named %s' % key)
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True,
                        help='the private fantasy hockey repo')
    parser.add_argument('--check', action='store_true',
                        help='report what would be written, write nothing')
    args = parser.parse_args()

    if not os.path.isdir(args.source):
        raise SystemExit('No such directory: %s' % args.source)

    alias, by_season, ids = build_aliases(args.source)
    if not alias:
        raise SystemExit('Found no manager-to-team mapping under %s' % args.source)

    current = load_current(args.source, alias)
    if not current:
        raise SystemExit('Found no matchup_long_*.csv under %s' % args.source)
    current_key = next((k for k, v in SEASON_LABEL.items()
                        if v == current['label']), None)

    payload = {
        'league': 'Bettman Cometh',
        'current': current,
        'history': load_history(args.source, ids, alias, current_key),
        'rosters': load_rosters(args.source, alias),
    }

    leaked = audit(payload, alias)
    if leaked:
        raise SystemExit('Refusing to write: manager names present in output: %s'
                         % ', '.join(leaked))

    print('Seasons: %s' % ', '.join(
        [payload['current']['label']] + [s['label'] for s in payload['history']]))
    print('Teams:   %d, %d managers aliased' % (len(current['teams']), len(alias)))
    print('Matches: %d current, %d historical'
          % (len(current['matchups']),
             sum(len(s['matchups']) for s in payload['history'])))
    print('Rosters: %d teams' % len(payload['rosters']))
    print('Audit:   no manager names in output')

    if args.check:
        print('(--check: nothing written)')
        return 0

    os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
    with io.open(OUTPUT, 'w', encoding='utf-8', newline='') as fh:
        fh.write(json.dumps(payload, indent=1, ensure_ascii=False,
                            sort_keys=True) + '\n')
    print('Wrote %s' % OUTPUT)
    return 0


if __name__ == '__main__':
    sys.exit(main())
