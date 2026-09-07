#!/usr/bin/env python3
"""Turn the private fantasy baseball exports into anonymised data for the site.

Same contract as tools/extract_hockey.py: the Yahoo exports carry manager
names and email addresses, so they stay local, and only team-name-keyed data
is written out. The manager column joins the seasons together and is then
dropped. The output is audited before it is written and the build refuses to
publish anything that fails.

Head-to-head category scoring, so a week is twelve small contests rather than
one. The category set and its directions were not assumed: they were fitted
against the recorded results and reproduce them exactly on every completed
week, which is what makes an all-play calculation trustworthy here.

The 2026 exports label every team by its manager, and the one file holding
that season's real team names carries no key to join on. Managers therefore
take the last team name they can be matched to, and anyone with no prior
season gets a placeholder until --alias-override supplies one.

Usage:
    python tools/extract_baseball.py --source C:/Users/cdmac/sacrifice_bundt_fantasy_baseball_2026 \n        --source C:/Users/cdmac/Downloads/YahooFantasy
    python tools/extract_baseball.py --source ... --check
    python tools/extract_baseball.py --source ... --alias-override my_aliases.csv
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
OUTPUT = os.path.join(ROOT, 'BB', 'data', 'baseball.json')

# Optional, and safe to commit: it maps one team name to another and so holds
# no manager names. The 2026 exports label every team by its manager and the
# only file with that season's real names has no key to join on, so without
# this a renamed team keeps last season's name on the page.
RENAMES = os.path.join(ROOT, 'BB', 'data', 'renames.csv')

# Fitted against scores.csv, exact on every completed week. OUT is IP*3 on
# every row, so only one of the pair is a scoring category.
# Every category either season set has used, and which way each is won.
# 2024-25 scored AVG, W and SV; 2026 replaced them with OBP, QS and SVH and
# added TB and OUT. Directions were confirmed by reproducing each season's
# recorded category record exactly.
CATS_HIGH = ['R', 'HR', 'RBI', 'SB', 'AVG', 'W', 'SV', 'K',
             'TB', 'OBP', 'QS', 'SVH', 'OUT']
CATS_LOW = ['ERA', 'WHIP']
CATEGORIES = CATS_HIGH + CATS_LOW

# The same category is spelled differently between the two 2026 exports.
CANON = {'SV+H': 'SVH'}


def canon(name):
    return CANON.get(name, name)


def read_csv(path):
    with io.open(path, encoding='utf-8-sig', errors='replace', newline='') as fh:
        return list(csv.DictReader(fh))


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
    return [h for h in hits if '.venv' not in h and 'site-packages' not in h
            and 'yfbhstat' not in h]


def num(row, key):
    try:
        return float(row[key])
    except (TypeError, ValueError, KeyError):
        return None


LEAGUE_SEASON = {
    '431.l.148152': '2024',
    '458.l.22655': '2025',
    '469.l.9715': '2026',
}


def league_key(path):
    """
    Pull '458.l.22655' out of a filename.
    """
    m = re.search(r'(\d+)[._]l[._](\d+)', os.path.basename(path))
    return '%s.l.%s' % (m.group(1), m.group(2)) if m else None


def build_aliases(source, override_path):
    """
    manager -> published team name, newest known name winning.

    Keyed by league rather than by filename order: there is a Team export for
    both 2024 and 2025, and letting the second overwrite the first silently
    cost the 2024-only managers their names.

    Within a season the matchup export wins over the team export. Names churn
    during a season -- 2025's team export has one manager as 'Montreal Expose'
    where the matchups have them as 'Gloucester Glizzy Gulls' -- and the
    matchup file agrees with the end-of-season team list.
    """
    seasons = {}

    def record(season, manager, name, weak):
        if not (season and manager and name):
            return
        slot = seasons.setdefault(season, {})
        if manager not in slot or not weak:
            slot[manager] = name

    #
    # Team id -> the season's real team name, and team id -> the manager
    # string the rest of that season's files use. Those two are joined on the
    # id rather than on a name, which is what makes the awkward cases fall
    # out for free: the team export writes one manager as 'Brandon Pineda'
    # where the rosters say 'Brandon', another as 'Phillip' against 'Phil',
    # and hides a third behind Yahoo's privacy setting entirely.
    #
    names_by_id, managers_by_id = {}, {}

    for path in find(source, '*-Team.csv'):
        season = LEAGUE_SEASON.get(league_key(path))
        if not season:
            continue
        for row in read_csv(path):
            team_id = (row.get('ID') or '').strip()
            name = (row.get('Name') or '').strip()
            if team_id and name:
                names_by_id[team_id] = name
            record(season, (row.get('Manager') or '').strip(), name, weak=True)

    for pattern, id_col, mgr_col in (('rosters.csv', 'team_key', 'team_name'),
                                     ('*Matchup-API.csv', 'Team_ID', 'Player')):
        for path in find(source, pattern):
            for row in read_csv(path):
                team_id = (row.get(id_col) or '').strip()
                manager = (row.get(mgr_col) or '').strip()
                if team_id and manager:
                    managers_by_id.setdefault(team_id, manager)

    for team_id, manager in managers_by_id.items():
        season = LEAGUE_SEASON.get(team_id.rsplit('.t.', 1)[0])
        name = names_by_id.get(team_id)
        if season and name and manager != name:
            record(season, manager, name, weak=False)

    for path in find(source, '*Matchup-API.csv'):
        season = LEAGUE_SEASON.get(league_key(path))
        for row in read_csv(path):
            manager = (row.get('Player') or '').strip()
            name = (row.get('Team_Name') or '').strip()
            if manager and name and manager != name:
                record(season, manager, name, weak=True)

    alias = {}
    for season in sorted(seasons):          # newer overwrites older
        alias.update(seasons[season])

    #
    # Anyone still playing is published under the name they use now. Anyone
    # who is not keeps their username rather than whatever team name they
    # last happened to hold.
    #
    latest = seasons.get(max(seasons)) if seasons else {}
    for manager in list(alias):
        if manager not in (latest or {}):
            alias[manager] = manager

    if override_path:
        for row in read_csv(override_path):
            manager = (row.get('manager') or '').strip()
            name = (row.get('team') or '').strip()
            if manager and name:
                alias[manager] = name

    return alias, seasons


def load_renames():
    """
    was -> now. Applied to the published name rather than to the alias map,
    so that it reaches the placeholder teams as well, which never enter that
    map at all.
    """
    renames = {}
    if os.path.exists(RENAMES):
        for row in read_csv(RENAMES):
            was = (row.get('was') or '').strip()
            now = (row.get('now') or '').strip()
            if was and now:
                renames[was] = now
    return renames


RENAME_MAP = {}


def alias_for(manager, alias, placeholders):
    """
    A manager with no team name anywhere gets a stable placeholder rather
    than their own name, which would defeat the point of the exercise.
    """
    if manager in alias:
        name = alias[manager]
    else:
        if manager not in placeholders:
            placeholders[manager] = 'Unnamed team %d' % (len(placeholders) + 1)
        name = placeholders[manager]
    return RENAME_MAP.get(name, name)


def season_categories(columns):
    """
    Which scoring categories a season actually used, read off its own
    columns. The set changed between 2025 and 2026 -- AVG, W and SV gave way
    to OBP, QS and SVH, with TB and OUT added -- so it cannot be hardcoded.
    """
    found = [(c[len('Team 1 '):], canon(c[len('Team 1 '):])) for c in columns
             if c.startswith('Team 1 ')
             and c[len('Team 1 '):] not in ('ID', 'Name', 'Points')]
    return ([(raw, name) for raw, name in found if name in CATS_HIGH],
            [(raw, name) for raw, name in found if name in CATS_LOW])


def load_seasons(source, alias, placeholders):
    """
    Every season as weekly category lines, from the wide matchup exports.

    Playoff and consolation weeks are dropped. The league history the
    notebooks compute counts the regular season only, and including the rest
    put every team roughly three weeks of categories ahead of it; excluding
    them reproduces the recorded category record exactly for all eight teams
    in both completed seasons, which is what says the direction of each
    category here is right.
    """
    best = {}
    for path in find(source, '*-Matchup*.csv'):
        key = league_key(path)
        if key not in LEAGUE_SEASON:
            continue
        # The same export can appear twice, once as a "(1)" duplicate.
        if key not in best or os.path.getsize(path) > os.path.getsize(best[key]):
            best[key] = path

    seasons = []
    for key, path in sorted(best.items(), key=lambda kv: LEAGUE_SEASON[kv[0]]):
        rows = read_csv(path)
        if not rows:
            continue
        hi_pairs, lo_pairs = season_categories(rows[0].keys())
        if not hi_pairs:
            continue
        hi = [name for _, name in hi_pairs]
        lo = [name for _, name in lo_pairs]

        ids = {}
        for team_path in find(source, '*-Team.csv'):
            if league_key(team_path) != key:
                continue
            for row in read_csv(team_path):
                manager = (row.get('Manager') or '').strip()
                team_id = (row.get('ID') or '').strip()
                name = (row.get('Name') or '').strip()
                if not team_id:
                    continue
                ids[team_id] = (alias_for(manager, alias, placeholders)
                                if manager and manager in alias else name)

        weeks = {}
        for row in rows:
            if (row.get('Playoff') or '').strip().lower() == 'true':
                continue
            if (row.get('Consolation') or '').strip().lower() == 'true':
                continue
            try:
                week = int(float(row['Week']))
            except (TypeError, ValueError):
                continue
            home = ids.get((row.get('Team 1 ID') or '').strip())
            away = ids.get((row.get('Team 2 ID') or '').strip())
            if not home or not away:
                continue
            done = (row.get('Complete') or '').strip().lower() == 'true'
            lines = {home: {}, away: {}}
            for raw, cat in hi_pairs + lo_pairs:
                for side, team in (('Team 1 ', home), ('Team 2 ', away)):
                    value = num(row, side + raw)
                    if value is not None:
                        lines[team][cat] = round(value, 4)
            weeks.setdefault(week, []).extend([
                {'t': home, 'o': away, 'c': lines[home], 'done': done},
                {'t': away, 'o': home, 'c': lines[away], 'done': done},
            ])

        if not weeks:
            continue
        teams = sorted({e['t'] for wk in weeks.values() for e in wk})
        played = [w for w, entries in weeks.items()
                  if all(e['done'] for e in entries)]
        seasons.append({
            'label': LEAGUE_SEASON[key],
            'teams': teams,
            'weeks': [{'w': w, 'entries': weeks[w]} for w in sorted(weeks)],
            'complete_through': max(played) if played else 0,
            'cats_high': hi, 'cats_low': lo,
        })
    return seasons


def load_history(source, alias, placeholders):
    """
    Finishes, category records and the career table, from the league history
    the notebooks already compute.
    """
    history, alltime = [], []

    for path in find(source, 'league_history_records.csv'):
        for row in read_csv(path):
            manager = (row.get('manager') or '').strip()
            if not manager:
                continue
            entry = {'season': row.get('season'),
                     'team': alias_for(manager, alias, placeholders),
                     'finish': int(float(row['finish'])) if row.get('finish') else None,
                     'of': int(float(row['n_teams'])) if row.get('n_teams') else None,
                     'win_pct': num(row, 'win_pct'),
                     'cw': num(row, 'cW'), 'cl': num(row, 'cL'), 'ct': num(row, 'cT'),
                     'reg_champ': (row.get('is_reg_champ') == 'True'),
                     'champion': (row.get('is_champion') == 'True'),
                     'status': row.get('status'),
                     'cats': {}}
            for key, value in row.items():
                if key.startswith('cat_') and key.endswith('_pct'):
                    got = num(row, key)
                    if got is not None:
                        entry['cats'][key[4:-4]] = round(got, 4)
            history.append(entry)
        break

    for path in find(source, 'league_history_alltime.csv'):
        for row in read_csv(path):
            manager = (row.get('manager') or '').strip()
            if not manager:
                continue
            alltime.append({
                'team': alias_for(manager, alias, placeholders),
                'seasons': int(float(row['seasons'])) if row.get('seasons') else None,
                'cw': num(row, 'tot_cW'), 'cl': num(row, 'tot_cL'),
                'ct': num(row, 'tot_cT'),
                'avg_finish': num(row, 'avg_finish'),
                'best_finish': num(row, 'best_finish'),
                'reg_titles': num(row, 'reg_titles'), 'titles': num(row, 'titles'),
                'win_pct': num(row, 'career_win_pct'),
            })
        break

    return history, alltime


def audit(payload, alias, placeholders):
    """
    Refuse to write anything that identifies a manager.

    As with hockey the check is on exact identity rather than substrings:
    several managers share a first name with a team name someone chose.
    """
    # Case-insensitive: a manager name may appear only where it was
    # deliberately chosen as that person's published identity.
    managers = {m.lower() for m in list(alias) + list(placeholders) if m}
    published = {v.lower() for v in
                 list(alias.values()) + list(placeholders.values())}
    problems = []

    identities = set()
    for season in payload.get('seasons', []):
        identities.update(season['teams'])
    for entry in payload.get('history', []):
        identities.add(entry['team'])
    for entry in payload.get('alltime', []):
        identities.add(entry['team'])
    for season in payload.get('seasons', []):
        for week in season['weeks']:
            for e in week['entries']:
                identities.add(e['t'])
                identities.add(e['o'])

    for who in sorted({i.lower() for i in identities} & managers):
        if who not in published:
            problems.append('used as a team identity: %s' % who)

    blob = json.dumps(payload)
    for hit in set(re.findall(r'[\w.+-]+@[\w-]+\.[\w.]+', blob)):
        problems.append('email address: %s' % hit)
    for key in ('manager', 'manager_name', 'manager_email', 'email', 'Player'):
        if '"%s"' % key in blob:
            problems.append('field named %s' % key)
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, action='append',
                        metavar='DIR',
                        help='an export tree; repeat for more than one')
    parser.add_argument('--alias-override', dest='override',
                        help='local csv of manager,team for anyone the exports '
                             'cannot name. Keep it out of the repo: it is the '
                             'key that would undo the anonymising.')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()

    for source in args.source:
        if not os.path.isdir(source):
            raise SystemExit('No such directory: %s' % source)

    global RENAME_MAP
    RENAME_MAP = load_renames()
    alias, seasons = build_aliases(args.source, args.override)
    if not alias:
        raise SystemExit('Found no manager-to-team mapping under %s' % args.source)

    placeholders = {}
    seasons_data = load_seasons(args.source, alias, placeholders)
    history, alltime = load_history(args.source, alias, placeholders)
    if not seasons_data and not history:
        raise SystemExit('Found neither weekly nor history data')

    payload = {'league': 'Sacrifice Bundt', 'seasons': seasons_data,
               'current': seasons_data[-1] if seasons_data else None,
               'history': history, 'alltime': alltime}

    problems = audit(payload, alias, placeholders)
    if problems:
        raise SystemExit('Refusing to write:\n  ' + '\n  '.join(problems))

    print('Aliases: %d from exports (%s)'
          % (len(alias), ', '.join('%s:%d' % (y, len(p))
                                   for y, p in sorted(seasons.items()))))
    if placeholders:
        shown = sorted(RENAME_MAP.get(v, v) for v in placeholders.values())
        print('         %d without a team name in the exports -> %s'
              % (len(placeholders), ', '.join(shown)))
    if RENAME_MAP:
        print('Renames: %d applied from BB/data/renames.csv' % len(RENAME_MAP))
    for season in seasons_data:
        print('Season:  %s, %2d teams, %2d regular-season weeks (complete '
              'through week %d), %d categories: %s'
              % (season['label'], len(season['teams']), len(season['weeks']),
                 season['complete_through'],
                 len(season['cats_high']) + len(season['cats_low']),
                 ', '.join(season['cats_high'] + season['cats_low'])))
    print('History: %d season-records, %d career rows' % (len(history), len(alltime)))
    print('Audit:   clean')

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
