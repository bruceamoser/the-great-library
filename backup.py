"""Consistent online SQLite backup. Use a destination on another disk."""
import argparse, sqlite3
from pathlib import Path

def backup(source, target):
    target = Path(target)
    if target.exists():
        raise FileExistsError('Choose a new backup filename; existing backups are preserved.')
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f'file:{Path(source).resolve()}?mode=ro', uri=True) as src:
        with sqlite3.connect(target) as dst:
            src.backup(dst, pages=256, sleep=.05)
    return target

if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('source'); p.add_argument('destination')
    a=p.parse_args(); print(backup(a.source,a.destination))
