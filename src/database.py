"""Версия SQLite, согласованные резервные копии и транзакционные изменения."""

import os
import sqlite3
import tempfile
from contextlib import contextmanager, closing
from datetime import datetime, timezone, timedelta
from pathlib import Path
from uuid import uuid4

from flask import current_app, g


SCHEMA_VERSION = 1
MAX_IMPORT_SIZE = 32 * 1024 * 1024
BACKUP_LIMIT = 20
MOSCOW_TIME = timezone(timedelta(hours=3), 'МСК')


class BackupError(RuntimeError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def get_db():
    if 'db' not in g:
        g.db = sqlite3.connect(current_app.config['DATABASE'], timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


def database_version():
    db = get_db()
    schema = db.execute('PRAGMA user_version').fetchone()[0]
    row = db.execute('SELECT revision, updated_at FROM db_meta WHERE id=1').fetchone()
    return dict(schema=schema, revision=row['revision'], updated_at=row['updated_at'])


def file_info(path):
    changed = datetime.fromtimestamp(path.stat().st_mtime, MOSCOW_TIME)
    return dict(name=path.name, modified_at=changed.isoformat(),
                modified_display=changed.strftime('%d.%m.%Y %H:%M:%S МСК'))


def database_file_info():
    return file_info(Path(current_app.config['DATABASE']))


def backup_paths():
    directory = Path(current_app.config['DATABASE']).parent / 'backups'
    entries = []
    for path in directory.glob('*.sqlite3'):
        try:
            if path.is_file() and not path.is_symlink():
                entries.append((path.stat().st_mtime_ns, path.name, path))
        except FileNotFoundError:
            continue
    return [entry[2] for entry in sorted(entries, reverse=True)]


def prune_backups(protected=None):
    paths = backup_paths()
    keep = set(paths[:BACKUP_LIMIT])
    if protected is not None and protected not in keep:
        keep = {protected, *paths[:BACKUP_LIMIT - 1]}
    for path in paths:
        if path not in keep:
            path.unlink()


def backup_history():
    history = []
    for path in backup_paths()[:BACKUP_LIMIT]:
        try:
            item = dict(file_info(path), schema=None, revision=None)
        except FileNotFoundError:
            continue
        try:
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)) as db:
                item['schema'] = db.execute('PRAGMA user_version').fetchone()[0]
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='db_meta'").fetchone():
                    row = db.execute('SELECT revision FROM db_meta WHERE id=1').fetchone()
                    item['revision'] = row[0] if row else None
        except sqlite3.Error:
            pass
        history.append(item)
    return history


def read_backup_file(filename):
    """Читаем только обычный файл из доступной истории, не произвольный путь."""
    if Path(filename).name != filename or '\\' in filename:
        raise FileNotFoundError(filename)
    candidates = {path.name: path for path in backup_paths()[:BACKUP_LIMIT]}
    path = candidates.get(filename)
    if path is None:
        raise FileNotFoundError(filename)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as file:
            return file.read()
    except OSError as error:
        raise FileNotFoundError(filename) from error


def export_database():
    """Согласованный снимок целой БД, без изменения текущих данных."""
    with closing(sqlite3.connect(':memory:')) as snapshot:
        get_db().backup(snapshot)
        schema = snapshot.execute('PRAGMA user_version').fetchone()[0]
        revision = snapshot.execute('SELECT revision FROM db_meta WHERE id=1').fetchone()[0]
        return snapshot.serialize(), schema, revision


def backup_database(reason):
    """Отдельный читатель видит состояние до незавершённой транзакции записи."""
    path = None
    try:
        directory = Path(current_app.config['DATABASE']).parent / 'backups'
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        db = get_db()
        schema = db.execute('PRAGMA user_version').fetchone()[0]
        has_meta = db.execute("SELECT 1 FROM sqlite_master WHERE name='db_meta' AND type='table'").fetchone()
        revision = db.execute('SELECT revision FROM db_meta WHERE id=1').fetchone()[0] if has_meta else 0
        path = directory / f'{name}-s{schema}-r{revision}-{reason}-{uuid4().hex[:8]}.sqlite3'
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        uri = Path(current_app.config['DATABASE']).resolve().as_uri() + '?mode=ro'
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as source:
            with closing(sqlite3.connect(path)) as target:
                source.backup(target)
        prune_backups(protected=path)
        return path
    except (OSError, sqlite3.Error) as error:
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise BackupError('Не удалось сохранить резервную копию или очистить историю. Изменения не сохранены.') from error


@contextmanager
def database_change(reason):
    db = get_db()
    db.execute('BEGIN IMMEDIATE')
    try:
        backup_database(reason)
        yield db
        db.execute('UPDATE db_meta SET revision=revision+1, updated_at=? WHERE id=1', (now(),))
        db.commit()
    except Exception:
        db.rollback()
        raise


def initialize_database(defaults):
    db = get_db()
    db.execute('BEGIN IMMEDIATE')
    try:
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version > SCHEMA_VERSION:
            raise ValueError(f'Версия схемы БД {version} новее поддерживаемой {SCHEMA_VERSION}.')
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if version == 0:
            if tables:
                backup_database('before-migration')
            db.execute('CREATE TABLE IF NOT EXISTS texts (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS admins ('
                       'id INTEGER PRIMARY KEY CHECK(id=1), username TEXT NOT NULL, '
                       'password_hash TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, '
                       'failures INTEGER NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE db_meta ('
                       'id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL, updated_at TEXT NOT NULL)')
            db.execute('INSERT INTO db_meta VALUES (1, 1, ?)', (now(),))
            db.executemany('INSERT OR IGNORE INTO texts (key, value) VALUES (?, ?)', defaults)
            db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        else:
            existing = {row[0] for row in db.execute('SELECT key FROM texts')}
            missing = [(key, value) for key, value in defaults if key not in existing]
            if missing:
                backup_database('before-seed')
                db.executemany('INSERT INTO texts (key, value) VALUES (?, ?)', missing)
                db.execute('UPDATE db_meta SET revision=revision+1, updated_at=? WHERE id=1', (now(),))
        prune_backups()
        db.commit()
    except Exception:
        db.rollback()
        raise


def read_import(upload, expected_keys):
    """Читаем только значения texts, не исполняя схему/триггеры загруженной базы."""
    content = upload.stream.read(MAX_IMPORT_SIZE + 1)
    if len(content) > MAX_IMPORT_SIZE:
        raise ValueError('Файл базы должен быть не больше 32 МиБ.')
    return read_database_texts(content, expected_keys)


def read_database_texts(content, expected_keys):
    """Общая валидация текстов для загрузки и восстановления локального снимка."""
    if not content.startswith(b'SQLite format 3\x00'):
        raise ValueError('Выберите корректный файл SQLite.')
    path = None
    try:
        with tempfile.NamedTemporaryFile(dir=current_app.instance_path, suffix='.sqlite3', delete=False) as file:
            file.write(content)
            path = Path(file.name)
        uri = path.resolve().as_uri() + '?mode=ro&immutable=1'
        with closing(sqlite3.connect(uri, uri=True)) as source:
            source.execute('PRAGMA trusted_schema=OFF')
            if source.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Файл базы повреждён.')
            version = source.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError('Версия схемы импортируемой базы не поддерживается.')
            table = source.execute("SELECT type FROM sqlite_master WHERE name='texts'").fetchone()
            if table != ('table',):
                raise ValueError('В базе отсутствует таблица текстов RACI.')
            rows = source.execute('SELECT key, value FROM texts LIMIT ?', (len(expected_keys) + 1,)).fetchall()
            if (len(rows) != len(expected_keys) or {row[0] for row in rows} != set(expected_keys)
                    or any(not isinstance(key, str) or not isinstance(value, str) for key, value in rows)):
                raise ValueError('База должна содержать ровно 12 текстовых полей RACI.')
            return dict(rows)
    except sqlite3.Error as error:
        raise ValueError('Не удалось прочитать тексты: файл SQLite повреждён или имеет неверную структуру.') from error
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
