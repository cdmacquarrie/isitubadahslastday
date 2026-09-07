#!/usr/bin/env python3
"""Update every generated page on the site from one folder of data.

Drop whatever you have exported into private-data/ -- Yahoo fantasy exports
for either sport, LearnedLeague league-wide exports, a seasons.json from the
Music League admin view -- and run this. It works out what each file is,
routes it to the right extractor, rebuilds every page and commits the result.

    python tools/update.py                 # do everything, commit, push
    python tools/update.py --check         # report what it found, change nothing
    python tools/update.py --no-push       # commit but leave the push to you

Nothing in private-data/ is ever committed: it is gitignored, and this repo is
public. The extractors read it and write anonymised, team-name-keyed json into
ML/data, HK/data and BB/data, which is what actually gets published.

LearnedLeague is the exception to "one folder". Its exports cover every player
in the league, not just the roster, so they live in the private ll-stats repo
and a workflow there publishes the page. This script will build it locally too
if you point --ll-repo at that checkout, which is quicker than waiting for CI
but leaves the two able to drift; it says so when it does.
"""
import argparse
import glob
import gzip
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_DATA = os.path.join(ROOT, 'private-data')
DEFAULT_LL_REPO = os.path.join(os.path.dirname(ROOT), '..', 'll-stats')

# League keys, so a Yahoo export can be routed by which game it belongs to
# rather than by guessing from a filename.
HOCKEY_LEAGUES = {'248.l.10100', '303.l.54154', '321.l.64686', '341.l.60609',
                  '352.l.33317', '453.l.82957', '465.l.20819'}
BASEBALL_LEAGUES = {'431.l.148152', '458.l.22655', '469.l.9715'}

# Where each sport's other exports normally live, kept as fallbacks so the
# script still works before everything has been moved into one folder.
LEGACY_SOURCES = {
    'hockey': [r'C:/Users/cdmac/bettman-cometh-fantasy-hockey-2026'],
    'baseball': [r'C:/Users/cdmac/sacrifice_bundt_fantasy_baseball_2026'],
}


def say(msg=''):
    print(msg)
    sys.stdout.flush()


def run(cmd, cwd=ROOT):
    """
    Run a command, returning (ok, output). Output is echoed on failure.
    """
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        say('    FAILED: %s' % ' '.join(cmd))
        for line in (proc.stdout + proc.stderr).strip().split('\n'):
            say('      %s' % line)
    return proc.returncode == 0, proc.stdout


def league_key(path):
    m = re.search(r'(\d+)[._]l[._](\d+)', os.path.basename(path))
    return '%s.l.%s' % (m.group(1), m.group(2)) if m else None


def survey(data_dir):
    """
    Work out what is sitting in the drop folder.

    Routing is by content rather than by folder, so it does not matter how
    the files are arranged underneath.
    """
    found = {'hockey': [], 'baseball': [], 'learnedleague': [], 'musicleague': []}
    if not os.path.isdir(data_dir):
        return found

    for path in glob.glob(os.path.join(data_dir, '**', '*'), recursive=True):
        if os.path.isdir(path):
            continue
        name = os.path.basename(path)
        key = league_key(name)

        if key in HOCKEY_LEAGUES:
            found['hockey'].append(path)
        elif key in BASEBALL_LEAGUES:
            found['baseball'].append(path)
        elif re.match(r'LL\d+_Leaguewide_.*\.csv(\.gz)?$', name) or \
                name in ('ll_qhist_export.json', 'll_referrals.json'):
            found['learnedleague'].append(path)
        elif name in ('seasons.json', 'durations.json'):
            found['musicleague'].append(path)
        elif name in ('live_rosters.csv', 'nhl_skater_stats.csv',
                      'nhl_goalie_stats.csv') or name.startswith('matchup_long_'):
            found['hockey'].append(path)
        elif name in ('scores.csv', 'rosters.csv') or name.startswith('Matchup_Data_'):
            found['baseball'].append(path)
    return found


def sources_for(sport, data_dir):
    """
    Every directory worth handing an extractor: the drop folder, plus the
    working repos, which still hold exports that were never moved.
    """
    # Order matters: within a season the last file read wins, so the drop
    # folder goes last, being the freshest. Passing it first quietly let a
    # stale snapshot in one of the working repos override a team that had
    # been renamed since.
    dirs = [path for path in LEGACY_SOURCES.get(sport, []) if os.path.isdir(path)]
    if os.path.isdir(data_dir):
        dirs.append(data_dir)
    return dirs


def update_musicleague(files, check):
    """
    The Music League data is produced by hand from the admin view, so this
    only moves it into place if a fresh copy has been dropped in.
    """
    moved = []
    for path in files:
        dest = os.path.join(ROOT, 'ML', 'data', os.path.basename(path))
        if os.path.exists(dest):
            with io.open(path, encoding='utf-8') as a, io.open(dest, encoding='utf-8') as b:
                if a.read() == b.read():
                    continue
        if not check:
            shutil.copyfile(path, dest)
        moved.append(os.path.basename(path))
    return moved


def build_learnedleague(ll_repo, data_dir, check):
    """
    Build the LearnedLeague page from whichever copy of the exports is to
    hand, preferring the drop folder.
    """
    generator = os.path.join(ll_repo, 'generator', 'friends_dashboard.py')
    roster = os.path.join(ll_repo, 'generator', 'friends_roster.csv')
    if not (os.path.exists(generator) and os.path.exists(roster)):
        return None, 'generator not found under %s' % ll_repo

    exports = glob.glob(os.path.join(data_dir, '**', 'LL*_Leaguewide_*'), recursive=True)
    source = 'the drop folder'
    if not exports:
        exports = glob.glob(os.path.join(ll_repo, 'data', 'LL*_Leaguewide_*'))
        source = 'the ll-stats repo'
    if not exports:
        return None, 'no league-wide exports found'
    extras = glob.glob(os.path.join(os.path.dirname(exports[0]), '*.json'))

    if check:
        return 'would build from %d exports in %s' % (len(exports), source), None

    work = tempfile.mkdtemp(prefix='ll-')
    try:
        for path in exports + extras:
            name = os.path.basename(path)
            if name.endswith('.gz'):
                with gzip.open(path, 'rb') as src:
                    with open(os.path.join(work, name[:-3]), 'wb') as dst:
                        shutil.copyfileobj(src, dst)
            else:
                shutil.copyfile(path, os.path.join(work, name))

        # The subtitle date has to come from the data, not the clock, or the
        # page changes every day whether or not anything else has.
        ok, stamp = run(['git', 'log', '-1', '--format=%cs'], cwd=ll_repo)
        as_of = stamp.strip() if ok and stamp.strip() else None

        built = os.path.join(work, 'built.html')
        cmd = [sys.executable, generator, '--data', work,
               '--roster', roster, '--out', built]
        if as_of:
            cmd += ['--as-of', as_of]
        ok, _ = run(cmd, cwd=ll_repo)
        if not ok:
            return None, 'generator failed'
        ok, _ = run([sys.executable, os.path.join(HERE, 'wrap_dashboard.py'),
                     built, os.path.join(ROOT, 'LL', 'index.html')])
        if not ok:
            return None, 'wrapping failed'
        return 'built from %d exports in %s' % (len(exports), source), None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def changed_paths():
    _, out = run(['git', 'status', '--porcelain'])
    # Parsed with a pattern rather than a fixed offset: the status field is
    # two characters and the gap after it varies, and slicing blindly had
    # been clipping the first letter off a path.
    paths = []
    for line in out.split('\n'):
        if line.strip():
            paths.append(re.sub(r'^..\s+', '', line).strip())
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data', default=DEFAULT_DATA, metavar='DIR',
                        help='the folder to read exports from (default private-data)')
    parser.add_argument('--ll-repo', dest='ll_repo', default=DEFAULT_LL_REPO,
                        metavar='DIR', help='checkout of the private ll-stats repo')
    parser.add_argument('--check', action='store_true',
                        help='report what would happen and change nothing')
    parser.add_argument('--no-push', dest='push', action='store_false',
                        help='commit but do not push')
    parser.add_argument('--message', '-m', default=None, help='commit message')
    args = parser.parse_args()

    data_dir = os.path.abspath(args.data)
    say('Reading %s' % data_dir)
    found = survey(data_dir)
    for sport in ('hockey', 'baseball', 'learnedleague', 'musicleague'):
        n = len(found[sport])
        say('  %-14s %s' % (sport, ('%d files' % n) if n else 'nothing found'))
    say()

    # --- extract -----------------------------------------------------------
    say('Extracting')
    for sport, script in (('hockey', 'extract_hockey.py'),
                          ('baseball', 'extract_baseball.py')):
        dirs = sources_for(sport, data_dir)
        if not dirs:
            say('  %-10s no source directories' % sport)
            continue
        cmd = [sys.executable, os.path.join(HERE, script)]
        for d in dirs:
            cmd += ['--source', d]
        if args.check:
            cmd.append('--check')
        ok, out = run(cmd)
        head = [l for l in out.strip().split('\n') if l.strip()]
        say('  %-10s %s' % (sport, head[-2] if len(head) > 1 else ('ok' if ok else 'failed')))

    moved = update_musicleague(found['musicleague'], args.check)
    say('  %-10s %s' % ('music', ', '.join(moved) if moved else 'no new data'))

    note, problem = build_learnedleague(args.ll_repo, data_dir, args.check)
    say('  %-10s %s' % ('learnedleague', note or ('skipped: ' + problem)))
    say()

    # --- build -------------------------------------------------------------
    say('Building pages')
    for label, script in (('music league', 'build_ml.py'),
                          ('hockey', 'build_hockey.py'),
                          ('baseball', 'build_baseball.py')):
        cmd = [sys.executable, os.path.join(HERE, script)]
        if args.check:
            cmd.append('--check')
        ok, out = run(cmd)
        say('  %-14s %s' % (label, out.strip().split('\n')[-1] if out.strip() else
                            ('ok' if ok else 'failed')))
    say()

    # --- publish -----------------------------------------------------------
    changes = [p for p in changed_paths() if not p.startswith('private-data')]
    if not changes:
        say('Nothing changed. The site is already up to date.')
        return 0

    say('Changed:')
    for path in changes:
        say('  %s' % path)
    say()

    if args.check:
        say('(--check: nothing written, nothing committed)')
        return 0

    message = args.message or 'Update site data'
    ok, _ = run(['git', 'add', '-A'])
    ok = ok and run(['git', 'commit', '-m', message])[0]
    if not ok:
        say('Commit failed.')
        return 1
    say('Committed: %s' % message)

    if not args.push:
        say('Not pushing (--no-push).')
        return 0

    # The LearnedLeague workflow pushes to this same branch.
    for attempt in (1, 2, 3):
        if run(['git', 'push', 'origin', 'main'])[0]:
            say('Pushed. The site rebuilds in about a minute.')
            return 0
        say('  push rejected (attempt %d); rebasing' % attempt)
        run(['git', 'pull', '--rebase', 'origin', 'main'])
    say('Could not push after 3 attempts.')
    return 1


if __name__ == '__main__':
    sys.exit(main())
