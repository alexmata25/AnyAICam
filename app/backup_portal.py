import argparse
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from cloud_config import settings


def sqlite_backup(destination):
    source=Path(settings.sqlite_path); destination=Path(destination)
    if not source.exists(): raise FileNotFoundError(f'SQLite database not found: {source}')
    destination.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(source) as src,sqlite3.connect(destination) as dst: src.backup(dst)
    return destination


def sqlite_restore(source,destination=None):
    source=Path(source); destination=Path(destination or settings.sqlite_path)
    if not source.exists(): raise FileNotFoundError(f'Backup not found: {source}')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_suffix('.restore.tmp'); shutil.copy2(source,temporary)
    with sqlite3.connect(temporary) as db: db.execute('PRAGMA integrity_check').fetchone()
    temporary.replace(destination); return destination


def main():
    parser=argparse.ArgumentParser(description='AnyAiCam local portal database backup and restore')
    sub=parser.add_subparsers(dest='command',required=True)
    backup=sub.add_parser('backup'); backup.add_argument('destination',nargs='?',default=f'backups/anyaicam-{datetime.now():%Y%m%d-%H%M%S}.db')
    restore=sub.add_parser('restore'); restore.add_argument('source'); restore.add_argument('--destination')
    args=parser.parse_args()
    if settings.database_backend!='sqlite': raise SystemExit('PostgreSQL uses the documented pg_dump/pg_restore commands.')
    path=sqlite_backup(args.destination) if args.command=='backup' else sqlite_restore(args.source,args.destination)
    print(path)


if __name__=='__main__': main()
