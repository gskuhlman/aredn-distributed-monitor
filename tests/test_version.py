"""Build number (version.py)."""

import subprocess

import version


def git(repo, *args, env=None):
    subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, env=env)


def test_counts_commits_on_the_newest_day(tmp_path, monkeypatch):
    git(tmp_path, 'init', '-q')
    base = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@x', 'GIT_COMMITTER_NAME': 't',
            'GIT_COMMITTER_EMAIL': 't@x', 'TZ': 'UTC', 'PATH': '/usr/bin:/bin'}
    monkeypatch.setenv('TZ', 'UTC')
    for i, day in enumerate(['2026-11-10T12:00:00', '2026-11-11T09:00:00', '2026-11-11T10:00:00',
                             '2026-11-11T23:00:00']):
        (tmp_path / 'f').write_text(str(i))
        git(tmp_path, 'add', 'f')
        git(tmp_path, 'commit', '-q', '-m', str(i), env=dict(base, GIT_COMMITTER_DATE=day, GIT_AUTHOR_DATE=day))
    assert version.from_git(str(tmp_path)) == '26.11.11.3'


def test_env_wins_and_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(version, '_cached', None)
    monkeypatch.setenv('APP_VERSION', '26.01.02.7')
    assert version.get() == '26.01.02.7'
    assert version.from_git(str(tmp_path)) is None    # not a git repository
