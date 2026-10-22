"""Проверки авторизации и сохранения начальных текстов в изолированной SQLite."""

import re
import io
import sqlite3
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from flask import Flask, render_template
from src.admin import RACI_FIELDS, get_db, init_admin, raci_texts, set_admin
from src.database import (BackupError, database_change, database_version, MAX_IMPORT_SIZE,
                          backup_history, database_file_info, BACKUP_LIMIT)


class AdminTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.app = self.make_app()
        with self.app.app_context():
            set_admin('admin', 'test-password-for-admin')
        self.client = self.app.test_client()

    def make_app(self):
        app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / 'src' / 'templates'),
                    instance_path=self.directory.name)
        app.config['TESTING'] = True
        init_admin(app)

        @app.get('/')
        def index():
            return render_template('form.html', raci_texts=raci_texts())

        app.add_url_rule('/raci_generate', endpoint='generate_raci', view_func=lambda: '', methods=['POST'])

        return app

    def token(self, path='/admin/login'):
        html = self.client.get(path).get_data(as_text=True)
        return re.search(r'name="csrf_token" value="([^"]+)"', html)[1]

    def login(self, password='test-password-for-admin'):
        return self.client.post('/admin/login', data={
            'csrf_token': self.token(), 'username': 'admin', 'password': password,
        })

    def test_authentication_and_csrf(self):
        self.assertEqual(self.client.get('/admin').status_code, 302)
        self.assertEqual(self.client.post('/admin', data={}).status_code, 400)
        self.assertIn('Неверный логин', self.login('wrong').get_data(as_text=True))
        self.assertEqual(self.client.get('/admin').status_code, 302)
        self.assertEqual(self.login().status_code, 302)
        self.assertEqual(self.client.get('/admin').status_code, 200)
        self.assertEqual(self.client.post('/admin', data={'csrf_token': 'wrong'}).status_code, 400)
        token = self.token('/admin')
        self.assertEqual(self.client.post('/admin', data={'csrf_token': token}).status_code, 400)
        self.assertEqual(self.client.post('/admin/logout', data={'csrf_token': token}).status_code, 302)
        self.assertEqual(self.client.get('/admin').status_code, 302)

    def test_save_clear_escape_and_restart(self):
        self.login()
        values = {key: f'Новый текст\n{label}' for key, label, _ in RACI_FIELDS}
        values['raci-about'] = '</textarea><script>alert(1)</script>'
        values['raci-prerequisites'] = ''
        values['csrf_token'] = self.token('/admin')
        self.assertEqual(self.client.post('/admin', data=values).status_code, 302)
        with self.app.app_context():
            texts = raci_texts()
            self.assertEqual(len(texts), 12)
            for key, _, _ in RACI_FIELDS:
                self.assertEqual(texts[key], values[key])
            password_hash = get_db().execute('SELECT password_hash FROM admins').fetchone()[0]
            self.assertNotIn('test-password-for-admin', password_hash)
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('&lt;script&gt;', html)
        self.assertNotIn('<script>alert(1)</script>', html)
        restarted = self.make_app()
        with restarted.app_context():
            self.assertEqual(raci_texts()['raci-prerequisites'], '')
            self.assertEqual(raci_texts()['raci-about'], values['raci-about'])
        self.assertEqual(restarted.config['SECRET_KEY'], self.app.config['SECRET_KEY'])
        restarted_client = restarted.test_client()
        restarted_client.set_cookie('session', self.client.get_cookie('session').value)
        self.assertEqual(restarted_client.get('/admin').status_code, 200)

    def test_password_rotation_invalidates_session(self):
        self.login()
        with self.app.app_context():
            set_admin('admin', 'replacement-password')
        self.assertEqual(self.client.get('/admin').status_code, 302)
        self.assertEqual(self.login('replacement-password').status_code, 302)

    def test_login_throttling(self):
        for _ in range(5):
            self.login('wrong')
        self.assertIn('временно заблокирован', self.login().get_data(as_text=True))
        self.assertEqual(self.client.get('/admin').status_code, 302)

    def import_file(self, version=0, missing=False):
        path = Path(self.directory.name) / f'import-{version}-{missing}.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE texts (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            fields = RACI_FIELDS[:-1] if missing else RACI_FIELDS
            db.executemany('INSERT INTO texts VALUES (?, ?)', [(key, 'Импорт ' + label) for key, label, _ in fields])
            # Администратор из чужого файла не должен попасть в текущую базу.
            db.execute('CREATE TABLE admins (username TEXT)')
            db.execute("INSERT INTO admins VALUES ('чужой администратор')")
            db.execute(f'PRAGMA user_version={version}')
        return io.BytesIO(path.read_bytes())

    def test_backup_before_save_and_revision(self):
        self.login()
        token = self.token('/admin')
        with self.app.app_context():
            old_texts = raci_texts()
            old_version = database_version()
        data = {key: 'Изменено ' + key for key, _, _ in RACI_FIELDS}
        data['csrf_token'] = token
        self.assertEqual(self.client.post('/admin', data=data).status_code, 302)
        backups = list((Path(self.directory.name) / 'backups').glob('*before-text-save*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(dict(backup.execute('SELECT key, value FROM texts')), old_texts)
            self.assertEqual(backup.execute('SELECT revision FROM db_meta').fetchone()[0], old_version['revision'])
        with self.app.app_context():
            self.assertEqual(database_version()['revision'], old_version['revision'] + 1)
        self.assertIn('Версия схемы БД', self.client.get('/admin').get_data(as_text=True))
        restarted = self.make_app()
        with restarted.app_context():
            self.assertEqual(database_version()['revision'], old_version['revision'] + 1)

    def test_backup_failure_prevents_save(self):
        self.login()
        token = self.token('/admin')
        with self.app.app_context():
            old_texts, old_version = raci_texts(), database_version()
        data = {key: 'Не сохранять' for key, _, _ in RACI_FIELDS}
        with patch('src.database.backup_database', side_effect=BackupError('Ошибка backup')):
            response = self.client.post('/admin', data={**data, 'csrf_token': token})
        self.assertEqual(response.status_code, 503)
        with self.app.app_context():
            self.assertEqual(raci_texts(), old_texts)
            self.assertEqual(database_version(), old_version)

    def test_transaction_failure_rolls_back(self):
        with self.app.app_context():
            old_texts, old_version = raci_texts(), database_version()
            with self.assertRaises(RuntimeError):
                with database_change('test-rollback') as db:
                    db.execute("UPDATE texts SET value='Не сохранять'")
                    raise RuntimeError('Ошибка записи')
            self.assertEqual(raci_texts(), old_texts)
            self.assertEqual(database_version(), old_version)

    def test_import_texts_preserves_admin_session_and_revision(self):
        self.login()
        for version in (0, 1):
            with self.subTest(version=version):
                with self.app.app_context():
                    before = database_version()
                    old_admin = tuple(get_db().execute('SELECT * FROM admins').fetchone())
                    old_texts = raci_texts()
                response = self.client.post('/admin/import', data={
                    'csrf_token': self.token('/admin'), 'database': (self.import_file(version), 'import.sqlite3'),
                }, content_type='multipart/form-data')
                self.assertEqual(response.status_code, 302)
                with self.app.app_context():
                    self.assertEqual(database_version()['revision'], before['revision'] + 1)
                    self.assertEqual(tuple(get_db().execute('SELECT * FROM admins').fetchone()), old_admin)
                    self.assertTrue(all(value.startswith('Импорт ') for value in raci_texts().values()))
                self.assertEqual(self.client.get('/admin').status_code, 200)
                backup = sorted((Path(self.directory.name) / 'backups').glob('*before-import*.sqlite3'))[-1]
                with sqlite3.connect(backup) as db:
                    self.assertEqual(dict(db.execute('SELECT key, value FROM texts')), old_texts)

    def test_invalid_import_and_auth(self):
        self.assertEqual(self.client.post('/admin/import', data={}).status_code, 400)
        token = self.token()
        self.assertEqual(self.client.post('/admin/import', data={'csrf_token': token}).status_code, 302)
        self.login()
        invalid_files = [io.BytesIO(b'not sqlite'), self.import_file(99), self.import_file(0, missing=True),
                         io.BytesIO(b'SQLite format 3\x00' + b'x' * 1000)]
        with self.app.app_context():
            old_texts, old_version = raci_texts(), database_version()
        for file in invalid_files:
            response = self.client.post('/admin/import', data={
                'csrf_token': self.token('/admin'), 'database': (file, 'bad.sqlite3'),
            }, content_type='multipart/form-data')
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post('/admin/import', data={
            'csrf_token': 'wrong', 'database': (io.BytesIO(b'data'), 'test.db'),
        }).status_code, 400)
        with self.app.app_context():
            self.assertEqual(raci_texts(), old_texts)
            self.assertEqual(database_version(), old_version)

    def test_oversized_import_is_rejected(self):
        self.login()
        response = self.client.post('/admin/import', data={
            'csrf_token': self.token('/admin'),
            'database': (io.BytesIO(b'SQLite format 3\x00' + b'x' * MAX_IMPORT_SIZE), 'large.db'),
        }, content_type='multipart/form-data')
        self.assertEqual(response.status_code, 400)
        response.request.environ['wsgi.input'].close()

    def test_migration_backup_and_future_schema(self):
        # Моделируем прежнюю БД без метаданных; данные/пароль сохраняются при миграции.
        path = Path(self.directory.name) / 'app.sqlite3'
        with sqlite3.connect(path) as db:
            db.execute('DROP TABLE db_meta')
            db.execute('PRAGMA user_version=0')
            old_texts = dict(db.execute('SELECT key, value FROM texts'))
            old_admin = db.execute('SELECT * FROM admins').fetchone()
        restarted = self.make_app()
        backups = list((Path(self.directory.name) / 'backups').glob('*before-migration*.sqlite3'))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 0)
        with restarted.app_context():
            self.assertEqual(database_version()['schema'], 1)
            self.assertEqual(raci_texts(), old_texts)
            self.assertEqual(tuple(get_db().execute('SELECT * FROM admins').fetchone()), old_admin)
        with sqlite3.connect(path) as db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaisesRegex(ValueError, 'новее поддерживаемой'):
            self.make_app()
        with sqlite3.connect(path) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 99)

    def test_download_requires_login(self):
        response = self.client.get('/admin/download')
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers['Location'].endswith('/admin/login'))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_download_current_snapshot_without_writes(self):
        self.login()
        with self.app.app_context():
            with database_change('prepare-download') as db:
                db.execute("UPDATE texts SET value='Актуальный текст' WHERE key='raci-about'")
            expected_texts = raci_texts()
            expected_version = database_version()
            expected_admins = [tuple(row) for row in get_db().execute('SELECT * FROM admins')]
        backups_before = set((Path(self.directory.name) / 'backups').glob('*.sqlite3'))
        response = self.client.get('/admin/download')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, 'application/vnd.sqlite3')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertIn(f's1-r{expected_version["revision"]}.sqlite3', response.headers['Content-Disposition'])
        snapshot = Path(self.directory.name) / 'download.sqlite3'
        snapshot.write_bytes(response.data)
        with sqlite3.connect(snapshot) as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
            self.assertEqual(dict(db.execute('SELECT key, value FROM texts')), expected_texts)
            self.assertEqual(db.execute('SELECT * FROM admins').fetchall(), expected_admins)
            self.assertEqual(db.execute('SELECT revision FROM db_meta').fetchone()[0], expected_version['revision'])
        with self.app.app_context():
            self.assertEqual(database_version(), expected_version)
        self.assertEqual(set((Path(self.directory.name) / 'backups').glob('*.sqlite3')), backups_before)
        self.assertIn('/admin/download', self.client.get('/admin').get_data(as_text=True))
        # Скачанный снимок совместим с импортом текстов в панели.
        imported = self.client.post('/admin/import', data={
            'csrf_token': self.token('/admin'),
            'database': (io.BytesIO(response.data), 'download.sqlite3'),
        }, content_type='multipart/form-data')
        self.assertEqual(imported.status_code, 302)

    def test_download_includes_committed_wal_data(self):
        self.login()
        with self.app.app_context():
            db = get_db()
            db.execute('PRAGMA journal_mode=WAL')
            with database_change('wal-download') as db:
                db.execute("UPDATE texts SET value='Данные из WAL' WHERE key='raci-about'")
            response = self.client.get('/admin/download')
            self.assertEqual(response.status_code, 200)
            path = Path(self.directory.name) / 'wal-download.sqlite3'
            path.write_bytes(response.data)
            with sqlite3.connect(path) as snapshot:
                self.assertEqual(snapshot.execute('PRAGMA quick_check').fetchone()[0], 'ok')
                self.assertEqual(snapshot.execute("SELECT value FROM texts WHERE key='raci-about'").fetchone()[0],
                                 'Данные из WAL')

    def test_backup_retention_keeps_last_twenty_revisions(self):
        with self.app.app_context():
            for index in range(25):
                with database_change('retention-test') as db:
                    db.execute("UPDATE texts SET value=? WHERE key='raci-about'", (str(index),))
            current = database_version()['revision']
            history = backup_history()
            self.assertEqual(len(history), BACKUP_LIMIT)
            self.assertEqual([item['revision'] for item in history], list(range(current - 1, current - 21, -1)))
            self.assertEqual(len(list((Path(self.directory.name) / 'backups').glob('*.sqlite3'))), 20)
            self.assertEqual(raci_texts()['raci-about'], '24')

    def test_startup_prunes_history_without_changing_database(self):
        directory = Path(self.directory.name) / 'backups'
        for old in directory.glob('*.sqlite3'):
            os.utime(old, ns=(1, 1))
        snapshot = (Path(self.directory.name) / 'app.sqlite3').read_bytes()
        for index in range(25):
            path = directory / f'fixture-{index:02d}.sqlite3'
            path.write_bytes(snapshot)
            os.utime(path, ns=(1700000000000000000 + index, 1700000000000000000 + index))
        note = directory / 'notes.txt'
        note.write_text('Сохранить')
        with self.app.app_context():
            old_version, old_texts = database_version(), raci_texts()
        restarted = self.make_app()
        with restarted.app_context():
            self.assertEqual(database_version(), old_version)
            self.assertEqual(raci_texts(), old_texts)
            self.assertEqual([item['name'] for item in backup_history()],
                             [f'fixture-{index:02d}.sqlite3' for index in range(24, 4, -1)])
        self.assertEqual(note.read_text(), 'Сохранить')

    def test_failed_snapshot_does_not_prune_existing_history(self):
        directory = Path(self.directory.name) / 'backups'
        before = set(directory.glob('*.sqlite3'))
        with self.app.app_context():
            db = get_db()
            revision = database_version()['revision']
            with patch('src.database.sqlite3.connect', side_effect=sqlite3.OperationalError('Ошибка копирования')):
                with self.assertRaises(BackupError):
                    with database_change('failed-retention'):
                        self.fail('Запись не должна начаться')
            self.assertFalse(db.in_transaction)
            self.assertEqual(database_version()['revision'], revision)
        self.assertEqual(set(directory.glob('*.sqlite3')), before)

    def test_admin_displays_file_dates_and_history(self):
        self.login()
        with self.app.app_context():
            file = database_file_info()
            history = backup_history()
        html = self.client.get('/admin').get_data(as_text=True)
        self.assertIn('Дата изменения файла БД', html)
        self.assertIn(file['modified_display'], html)
        self.assertIn(file['modified_at'], html)
        for item in history:
            self.assertIn(item['name'], html)
            self.assertIn(item['modified_display'], html)
        self.assertIn('МСК', html)

    def test_download_each_available_backup_without_changes(self):
        self.login()
        with self.app.app_context():
            for index in range(3):
                with database_change('download-history') as db:
                    db.execute("UPDATE texts SET value=? WHERE key='raci-about'", (f'Версия {index}',))
            history = backup_history()
            version = database_version()
        for item in history:
            with self.subTest(file=item['name']):
                response = self.client.get(f'/admin/backups/{quote(item["name"], safe="")}/download')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, (Path(self.directory.name) / 'backups' / item['name']).read_bytes())
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertIn(item['name'], response.headers['Content-Disposition'])
        html = self.client.get('/admin').get_data(as_text=True)
        for item in history:
            self.assertIn(f'/admin/backups/{item["name"]}/download', html)
            self.assertIn(f'/admin/backups/{item["name"]}/restore', html)
        self.assertIn('Восстановить тексты', html)
        with self.app.app_context():
            self.assertEqual(database_version(), version)
            self.assertEqual(backup_history(), history)

    def test_restore_selected_texts_with_backup_and_new_revision(self):
        with self.app.app_context():
            with database_change('old-texts') as db:
                db.execute("UPDATE texts SET value='Старые тексты'")
            with database_change('selected-version') as db:
                db.execute("UPDATE texts SET value='Новые тексты'")
            selected = next(item for item in backup_history() if 'selected-version' in item['name'])
            version = database_version()['revision']
            admin = tuple(get_db().execute('SELECT * FROM admins').fetchone())
        self.login()
        response = self.client.post(f'/admin/backups/{selected["name"]}/restore',
                                    data={'csrf_token': self.token('/admin')})
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertTrue(all(value == 'Старые тексты' for value in raci_texts().values()))
            self.assertEqual(database_version()['revision'], version + 1)
            self.assertEqual(tuple(get_db().execute('SELECT * FROM admins').fetchone()), admin)
        self.assertEqual(self.client.get('/admin').status_code, 200)
        copies = list((Path(self.directory.name) / 'backups').glob('*before-restore*.sqlite3'))
        self.assertEqual(len(copies), 1)
        with sqlite3.connect(copies[0]) as db:
            self.assertTrue(all(row[0] == 'Новые тексты' for row in db.execute('SELECT value FROM texts')))

    def test_restore_oldest_survives_retention_pruning(self):
        with self.app.app_context():
            for index in range(25):
                with database_change('oldest-restore') as db:
                    db.execute("UPDATE texts SET value=? WHERE key='raci-about'", (str(index),))
            selected = backup_history()[-1]
            version = database_version()['revision']
        selected_path = Path(self.directory.name) / 'backups' / selected['name']
        with sqlite3.connect(selected_path) as db:
            expected = dict(db.execute('SELECT key, value FROM texts'))
        self.login()
        response = self.client.post(f'/admin/backups/{selected["name"]}/restore',
                                    data={'csrf_token': self.token('/admin')})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(selected_path.exists())
        with self.app.app_context():
            self.assertEqual(raci_texts(), expected)
            self.assertEqual(database_version()['revision'], version + 1)
            self.assertEqual(len(backup_history()), 20)

    def test_backup_actions_auth_csrf_and_path_validation(self):
        filename = next((Path(self.directory.name) / 'backups').glob('*.sqlite3')).name
        download = f'/admin/backups/{filename}/download'
        restore = f'/admin/backups/{filename}/restore'
        self.assertEqual(self.client.get(download).status_code, 302)
        self.assertEqual(self.client.post(restore, data={'csrf_token': self.token()}).status_code, 302)
        self.login()
        self.assertEqual(self.client.post(restore, data={'csrf_token': 'wrong'}).status_code, 400)
        self.assertEqual(self.client.get('/admin/backups/missing.sqlite3/download').status_code, 404)
        self.assertEqual(self.client.post('/admin/backups/missing.sqlite3/restore',
                                        data={'csrf_token': self.token('/admin')}).status_code, 404)
        self.assertEqual(self.client.get('/admin/backups/..%2Fapp.sqlite3/download').status_code, 404)
        link = Path(self.directory.name) / 'backups' / 'link.sqlite3'
        link.symlink_to(Path(self.directory.name) / 'app.sqlite3')
        self.assertEqual(self.client.get('/admin/backups/link.sqlite3/download').status_code, 404)

    def test_restore_legacy_snapshot_and_invalid_snapshot(self):
        self.login()
        path = Path(self.directory.name) / 'backups' / 'legacy.sqlite3'
        path.write_bytes(self.import_file(0).getvalue())
        response = self.client.post('/admin/backups/legacy.sqlite3/restore',
                                    data={'csrf_token': self.token('/admin')})
        self.assertEqual(response.status_code, 302)
        with self.app.app_context():
            self.assertTrue(all(value.startswith('Импорт ') for value in raci_texts().values()))
            texts, version = raci_texts(), database_version()
        path = path.with_name('broken.sqlite3')
        path.write_bytes(b'not sqlite')
        response = self.client.post('/admin/backups/broken.sqlite3/restore',
                                    data={'csrf_token': self.token('/admin')})
        self.assertEqual(response.status_code, 400)
        with self.app.app_context():
            self.assertEqual(raci_texts(), texts)
            self.assertEqual(database_version(), version)

    def test_restore_backup_failure_preserves_current_texts(self):
        self.login()
        filename = next((Path(self.directory.name) / 'backups').glob('*.sqlite3')).name
        token = self.token('/admin')
        with self.app.app_context():
            texts, version = raci_texts(), database_version()
        with patch('src.database.backup_database', side_effect=BackupError('Ошибка backup')):
            response = self.client.post(f'/admin/backups/{filename}/restore', data={'csrf_token': token})
        self.assertEqual(response.status_code, 503)
        with self.app.app_context():
            self.assertEqual(raci_texts(), texts)
            self.assertEqual(database_version(), version)


if __name__ == '__main__':
    unittest.main()
