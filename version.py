"""
Build number shown in the header: yy.mm.dd.N, the date of the newest commit
(local time) and N = how many commits that day, so every update gets the next
number without anyone editing a file.

Where it comes from, first match wins:
  APP_VERSION   environment, set by the Docker build (the redeploy script runs
                `python3 version.py` against the clone and passes it as a
                build argument; the image has no .git)
  git           the clone this runs from (python app.py on a PC)
  'dev'         neither available

`python version.py [repo_dir]` prints it.
"""

import os
import subprocess
import sys

_cached = None


def from_git(repo_dir=None):
    repo_dir = repo_dir or os.path.dirname(os.path.abspath(__file__))
    try:
        out = subprocess.run(['git', '-C', repo_dir, 'log', '-n', '500', '--format=%cd',
                              '--date=format-local:%y.%m.%d', 'HEAD'],
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    days = out.stdout.split() if out.returncode == 0 else []
    if not days:
        return None
    return f'{days[0]}.{sum(1 for d in days if d == days[0])}'


def get():
    global _cached
    if _cached is None:
        _cached = (os.environ.get('APP_VERSION') or '').strip() or from_git() or 'dev'
    return _cached


if __name__ == '__main__':
    print(from_git(sys.argv[1] if len(sys.argv) > 1 else None) or 'dev')
