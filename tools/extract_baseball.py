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
    python tools/extract_baseball.py --source C:/Users/cdmac/sacrifice_bundt_fantasy_baseball_2026
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

# Fitted against scores.csv, exact on every completed week. OUT is IP*3 on
# every row, so only one of the pair is a scoring category.
CATS_HIGH = ['R', 'HR', 'RBI', 'SB', 'TB', 'OBP', 'K', 'QS', 'SVH', 'OUT']
CATS_LOW = ['ERA', 'WHIP']
CATEGORIES = CATS_HIGH + CATS_LOW


def read_csv(path):
    with io.open(path, encoding='utf-8-sig', errors='replace', newline='') as fh:
        return list(csv.DictReader(fh))


def find(source, pattern):
    hits = glob.glob(os.path.join(source, '**', pattern), recursive=True)
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

    for path in find(source, '*-Team.csv'):
        season = LEAGUE_SEASON.get(league_key(path))
        for row in read_csv(path):
            record(season, (row.get('Manager') or '').strip(),
                   (row.get('Name') or '').strip(), weak=True)

    for path in find(source, '*Matchup-API.csv'):
        season = LEAGUE_SEASON.get(league_key(path))
        for row in read_csv(path):
            manager = (row.get('Player') or '').strip()
            name = (row.get('Team_Name') or '').strip()
            if manager and name and manager != name:
                record(season, manager, name, weak=False)

    alias = {}
    for season in sorted(seasons):          # newer overwrites older
        alias.update(seasons[season])

    if override_path:
        for row in read_csv(override_path):
            manager = (row.get('manager') or '').strip()
            name = (row.get('team') or '').strip()
            if manager and name:
                alias[manager] = name

    return alias, seasons


def alias_for(manager, alias, placeholders):
    """
    A manager with no team name anywhere gets a stable placeholder rather
    than their own name, which would defeat the point of the exercise.
    """
    if manager in alias:
        return alias[manager]
    if manager not in placeholders:
        placeholders[manager] = 'Unnamed team %d' % (len(placeholders) + 1)
    return placeholders[manager]


def load_current(source, alias, placeholders):
    """
    The running season: one row per team per week, with the category line and
    the head-to-head result worked out from it.

    Weeks the league has not scored yet are carried but flagged, so the page
    can leave them out of the standings without losing the scoring data.
    """
    matchups = find(source, 'Matchup_Data_469_l_9715.csv')
    scores = find(source, 'scores.csv')
    if not matchups or not scores:
        return None

    recorded = {}
    for row in read_csv(sorted(scores)[-1]):
        try:
            week = int(float(row['Week']))
        except (TypeError, ValueError):
            continue
        won = num(row, 'Cats_Won') or 0
        lost = num(row, 'Cats_Lost') or 0
        tied = num(row, 'Cats_Tied') or 0
        recorded[(week, (row.get('Team_Name') or '').strip())] = \
            (won, lost, tied, (won + lost + tied) > 0)

    rows = read_csv(sorted(matchups)[-1])
    index = {}
    for row in rows:
        try:
            week = int(float(row['Week']))
        except (TypeError, ValueError):
            continue
        index[(week, (row.get('Team_Name') or '').strip())] = row

    weeks = {}
    for (week, manager), row in index.items():
        opponent = (row.get('Opponent_Name') or '').strip()
        other = index.get((week, opponent))
        if not other:
            continue
        line = {}
        for cat in CATEGORIES:
            value = num(row, cat)
            if value is not None:
                line[cat] = round(value, 4)
        mark = recorded.get((week, manager))
        weeks.setdefault(week, []).append({
            't': alias_for(manager, alias, placeholders),
            'o': alias_for(opponent, alias, placeholders),
            'c': line,
            'done': bool(mark and mark[3]),
        })

    teams = sorted({e['t'] for wk in weeks.values() for e in wk})
    played = sorted(w for w, entries in weeks.items()
                    if all(e['done'] for e in entries))
    return {'label': '2026', 'teams': teams,
            'weeks': [{'w': w, 'entries': weeks[w]} for w in sorted(weeks)],
            'complete_through': max(played) if played else 0,
            'cats_high': CATS_HIGH, 'cats_low': CATS_LOW}


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
    managers = {m for m in list(alias) + list(placeholders) if m}
    published = set(alias.values()) | set(placeholders.values())
    problems = []

    identities = set(payload['current']['teams'] if payload.get('current') else [])
    for entry in payload.get('history', []):
        identities.add(entry['team'])
    for entry in payload.get('alltime', []):
        identities.add(entry['team'])
    if payload.get('current'):
        for week in payload['current']['weeks']:
            for e in week['entries']:
                identities.add(e['t'])
                identities.add(e['o'])

    for who in sorted(identities & managers):
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
    parser.add_argument('--source', required=True)
    parser.add_argument('--alias-override', dest='override',
                        help='local csv of manager,team for anyone the exports '
                             'cannot name. Keep it out of the repo: it is the '
                             'key that would undo the anonymising.')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()

    if not os.path.isdir(args.source):
        raise SystemExit('No such directory: %s' % args.source)

    alias, seasons = build_aliases(args.source, args.override)
    if not alias:
        raise SystemExit('Found no manager-to-team mapping under %s' % args.source)

    placeholders = {}
    current = load_current(args.source, alias, placeholders)
    history, alltime = load_history(args.source, alias, placeholders)
    if not current and not history:
        raise SystemExit('Found neither current-season nor history data')

    payload = {'league': 'Sacrifice Bundt', 'current': current,
               'history': history, 'alltime': alltime}

    problems = audit(payload, alias, placeholders)
    if problems:
        raise SystemExit('Refusing to write:\n  ' + '\n  '.join(problems))

    print('Aliases: %d from exports (%s)'
          % (len(alias), ', '.join('%s:%d' % (y, len(p))
                                   for y, p in sorted(seasons.items()))))
    if placeholders:
        print('         %d without a team name anywhere -> %s'
              % (len(placeholders), ', '.join(sorted(placeholders.values()))))
    if current:
        print('Current: %s, %d teams, %d weeks, complete through week %d'
              % (current['label'], len(current['teams']), len(current['weeks']),
                 current['complete_through']))
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
