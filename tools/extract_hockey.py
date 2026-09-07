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
    python tools/extract_hockey.py --source C:/Users/cdmac/bettman-cometh-fantasy-hockey-2026 \n        --source C:/Users/cdmac/Downloads/YahooFantasy
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
# Doubles as the whitelist. The export folders hold both sports, and globbing
# for matchup files without checking the league pulled the baseball seasons
# into the hockey page. Seasons were dated from the transaction timestamps in
# each export rather than guessed from the game id.
SEASON_LABEL = {
    '248.l.10100': '2010-11',
    '303.l.54154': '2012-13',
    '321.l.64686': '2013-14',
    '341.l.60609': '2014-15',
    '352.l.33317': '2015-16',
    '453.l.82957': '2024-25',
    '465.l.20819': '2025-26',
}

# Yahoo writes this in place of a manager when the profile is private. It is
# not a name, and treating it as one would merge every private team in the
# league into a single identity.
HIDDEN = {'--hidden--', '-- hidden --', 'hidden'}


def real_manager(value):
    """
    A usable manager key, or None. Case is normalised because the older
    exports write 'drew' where the recent ones write 'Drew'.
    """
    name = (value or '').strip()
    if not name or name.lower().replace(' ', '') in {h.replace(' ', '') for h in HIDDEN}:
        return None
    return name.lower()


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


def find(sources, pattern):
    """
    Every match under any of the source trees, virtualenvs excluded.

    Takes a list because the exports live in more than one place: the working
    repo, and whatever folder a fresh download landed in.
    """
    if isinstance(sources, str):
        sources = [sources]
    hits = []
    for source in sources:
        hits.extend(glob.glob(os.path.join(source, '**', pattern), recursive=True))
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
    names = {}
    display = {}     # lowercased key -> the username as Yahoo spells it
    for path in find(source, '*-Team.csv'):
        key = league_key(path)
        if key not in SEASON_LABEL:
            continue
        rows = read_csv(path)
        pairs = {}
        for row in rows:
            manager = real_manager(row.get('Manager'))
            name = (row.get('Name') or '').strip()
            team_id = (row.get('ID') or '').strip()
            if manager:
                display[manager] = (row.get('Manager') or '').strip()
            if manager and name:
                pairs[manager] = name
            if manager and team_id:
                ids.setdefault(key, {})[team_id] = manager
            if team_id and name:
                names.setdefault(key, {})[team_id] = name
        if pairs:
            by_season.setdefault(key, {}).update(pairs)

    # live_rosters is the freshest source and covers the running season.
    for path in find(source, 'live_rosters.csv'):
        for row in read_csv(path):
            manager = real_manager(row.get('manager_name'))
            name = (row.get('team_name') or '').strip()
            key = league_key(row.get('team_key') or '')
            if key not in SEASON_LABEL:
                continue
            if manager:
                display[manager] = (row.get('manager_name') or '').strip()
            if manager and name:
                by_season.setdefault(key, {})[manager] = name
            team_id = (row.get('team_key') or '').strip()
            if manager and team_id:
                ids.setdefault(key, {})[team_id] = manager

    order = sorted(by_season, key=lambda k: SEASON_LABEL.get(k, k))
    # Newest label last, so the most recent team name wins the alias.
    alias = {}
    for key in order:                      # later seasons overwrite earlier
        alias.update(by_season[key])

    #
    # Anyone still playing is published under the name they use now. Anyone
    # who is not keeps their username instead of the last team name they
    # happened to hold, because some of those names from the early 2010s are
    # not worth carrying forward.
    #
    current = by_season.get(order[-1]) if order else {}
    for manager in list(alias):
        if manager not in (current or {}):
            alias[manager] = display.get(manager) or alias[manager]
    return alias, by_season, ids, names


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
        home = alias.get(real_manager(row.get('Team_Name')))
        away = alias.get(real_manager(row.get('Opp_Name')))
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


def load_history(source, ids, names, alias, skip_key):
    """
    Earlier seasons, from the wide-format matchup exports.

    Joined on team id rather than on the team name. Names are not stable
    within a season either -- one team is 'Bad habits' in the team export and
    'Trade Warriors' throughout the matchups, having been renamed partway
    through -- and matching on the name silently dropped every one of its
    games along with the team itself.
    """
    # One file per league: the same season can exist in more than one source
    # tree, and processing both would publish it twice. The largest export
    # wins, being the most complete.
    best = {}
    for path in find(source, '*-Matchup.csv'):
        key = league_key(path)
        if key not in SEASON_LABEL or key == skip_key:
            continue
        if key not in best or os.path.getsize(path) > os.path.getsize(best[key]):
            best[key] = path

    seasons = []
    for key, path in sorted(best.items(), key=lambda kv: SEASON_LABEL[kv[0]]):
        # That season's team id -> published name. A known manager carries
        # their stable alias across seasons; a team whose manager is private
        # simply keeps the name it used that year. Dropping those teams
        # instead would silently truncate everyone else's record, since the
        # matches played against them would go with them.
        to_alias = dict(names.get(key) or {})
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
            manager = real_manager(row.get('manager_name'))
            if manager and manager in alias:
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
    # Case matters here: the alias keys are lowercased and the usernames are
    # not, so a case-sensitive comparison would wave through exactly the
    # thing this is meant to catch.
    managers = {m.lower() for m in alias if m}
    published = {v.lower() for v in alias.values()}
    problems = []

    identities = set(payload['rosters'])
    for season in [payload['current']] + payload['history']:
        identities.update(season['teams'])
        for match in season['matchups']:
            identities.add(match['a'])
            identities.add(match['b'])

    for who in sorted({i.lower() for i in identities} & managers):
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
    parser.add_argument('--source', required=True, action='append',
                        metavar='DIR',
                        help='an export tree; repeat for more than one')
    parser.add_argument('--check', action='store_true',
                        help='report what would be written, write nothing')
    args = parser.parse_args()

    for source in args.source:
        if not os.path.isdir(source):
            raise SystemExit('No such directory: %s' % source)

    alias, by_season, ids, names = build_aliases(args.source)
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
        'history': load_history(args.source, ids, names, alias, current_key),
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
