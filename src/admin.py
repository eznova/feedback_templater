"""Хранение начальных текстов RACI и панель администратора."""

import secrets
import os
import time
from functools import wraps
from pathlib import Path
from io import BytesIO

import click
from flask import abort, current_app, g, redirect, render_template, request, session, url_for, send_file
from werkzeug.security import check_password_hash, generate_password_hash
from src.database import (get_db, initialize_database, database_change, database_version,
                      read_import, export_database, BackupError, MAX_IMPORT_SIZE,
                      database_file_info, backup_history, BACKUP_LIMIT,
                      read_backup_file, read_database_texts)


DEFAULT_RACI_TEXT = (
    'Повседневная практика показывает, что начало повседневной работы по формированию '
    'позиции представляет собой интересный эксперимент проверки направлений '
    'прогрессивного развития. Задача организации, в особенности же рамки и место '
    'обучения кадров влечет за собой процесс внедрения и модернизации системы '
    'обучения кадров, соответствует насущным потребностям.'
)

RACI_FIELDS = [
    ('raci-about', 'О матрице RACI', DEFAULT_RACI_TEXT),
    ('raci-prerequisites', 'Предпосылки внедрения', DEFAULT_RACI_TEXT),
]
for key, title in [
    ('recruitment', 'Подбор'), ('motivation', 'Мотивация'),
    ('competencies', 'Оценка компетенций'), ('culture', 'Корпоративная культура'),
    ('hr-documents', 'Кадровый документооборот'),
]:
    RACI_FIELDS.extend([
        (f'raci-{key}-before', f'{title} — Как было', ''),
        (f'raci-{key}-target', f'{title} — Как будет в целевой картине', DEFAULT_RACI_TEXT),
    ])


def raci_texts():
    return dict(get_db().execute('SELECT key, value FROM texts').fetchall())


def set_admin(username, password):
    if not username.strip() or len(password) < 12:
        raise ValueError('Нужен непустой логин и пароль длиной не менее 12 символов.')
    with database_change('before-admin-change') as db:
        db.execute(
            'INSERT INTO admins (id, username, password_hash) VALUES (1, ?, ?) '
            'ON CONFLICT(id) DO UPDATE SET username=excluded.username, '
            'password_hash=excluded.password_hash, version=admins.version+1, '
            'failures=0, locked_until=0',
            (username.strip(), generate_password_hash(password)),
        )


def init_admin(app):
    instance = Path(app.instance_path)
    instance.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not app.config.get('SECRET_KEY'):
        key_path = instance / 'session.key'
        try:
            with open(key_path, 'x', opener=lambda path, flags: os.open(path, flags, 0o600)) as file:
                file.write(secrets.token_hex(32))
        except FileExistsError:
            pass
        app.config['SECRET_KEY'] = key_path.read_text().strip()
    app.config.setdefault('DATABASE', str(instance / 'app.sqlite3'))
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
    with app.app_context():
        try:
            initialize_database([(key, default) for key, _, default in RACI_FIELDS])
        finally:
            db = g.pop('db', None)
            if db is not None:
                db.close()

    @app.teardown_appcontext
    def close_db(error=None):
        db = g.pop('db', None)
        if db is not None:
            db.close()

    def csrf_token():
        if 'csrf_token' not in session:
            session['csrf_token'] = secrets.token_hex(32)
        return session['csrf_token']

    app.jinja_env.globals['csrf_token'] = csrf_token

    @app.errorhandler(BackupError)
    def backup_failed(error):
        return render_template('admin_error.html', message=str(error)), 503

    @app.before_request
    def protect_admin_posts():
        if request.endpoint == 'admin_import':
            request.max_content_length = MAX_IMPORT_SIZE + 1024 * 1024
        if request.endpoint in {'admin_login', 'admin_editor', 'admin_logout', 'admin_import', 'admin_backup_restore'} and request.method == 'POST':
            expected = session.get('csrf_token', '')
            if not expected or not secrets.compare_digest(expected, request.form.get('csrf_token', '')):
                abort(400, description='Сессия формы истекла. Обновите страницу и повторите действие.')

    @app.after_request
    def prevent_admin_caching(response):
        if request.endpoint in {'admin_login', 'admin_editor', 'admin_logout', 'admin_import', 'admin_download',
                                'admin_backup_download', 'admin_backup_restore'}:
            response.headers['Cache-Control'] = 'no-store'
        return response

    def login_required(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            admin = get_db().execute('SELECT version FROM admins WHERE id=1').fetchone()
            if not admin or session.get('admin_version') != admin['version']:
                return redirect(url_for('admin_login'))
            return view(*args, **kwargs)
        return wrapped

    @app.route('/admin/login', methods=['GET', 'POST'])
    def admin_login():
        error = None
        if request.method == 'POST':
            db = get_db()
            admin = db.execute('SELECT * FROM admins WHERE id=1').fetchone()
            if admin and admin['locked_until'] > time.time():
                error = 'Вход временно заблокирован. Повторите через несколько минут.'
            elif (admin and secrets.compare_digest(request.form.get('username', '').encode(), admin['username'].encode())
                  and check_password_hash(admin['password_hash'], request.form.get('password', ''))):
                if admin['failures'] or admin['locked_until']:
                    with database_change('before-login-reset') as db:
                        db.execute('UPDATE admins SET failures=0, locked_until=0 WHERE id=1')
                session.clear()
                session['admin_version'] = admin['version']
                return redirect(url_for('admin_editor'))
            else:
                if admin:
                    failures = admin['failures'] + 1
                    with database_change('before-login-failure') as db:
                        db.execute('UPDATE admins SET failures=?, locked_until=? WHERE id=1',
                                   (failures if failures < 5 else 0, time.time() + 300 if failures >= 5 else 0))
                error = 'Неверный логин или пароль.'
        return render_template('admin_login.html', error=error)

    @app.route('/admin', methods=['GET', 'POST'])
    @login_required
    def admin_editor():
        if request.method == 'POST':
            if any(key not in request.form for key, _, _ in RACI_FIELDS):
                abort(400, description='Получены не все поля редактора.')
            with database_change('before-text-save') as db:
                db.executemany('UPDATE texts SET value=? WHERE key=?',
                               [(request.form[key], key) for key, _, _ in RACI_FIELDS])
            return redirect(url_for('admin_editor', saved='1'))
        return render_template('admin_editor.html', fields=RACI_FIELDS, texts=raci_texts(),
                               db_version=database_version(), db_file=database_file_info(),
                               backups=backup_history(), backup_limit=BACKUP_LIMIT)

    @app.get('/admin/download')
    @login_required
    def admin_download():
        content, schema, revision = export_database()
        return send_file(BytesIO(content), mimetype='application/vnd.sqlite3', as_attachment=True,
                         download_name=f'raci-database-s{schema}-r{revision}.sqlite3', max_age=0)

    def backup_content(filename):
        try:
            return read_backup_file(filename)
        except FileNotFoundError:
            abort(404, description='Эта версия БД недоступна. Обновите список резервных копий.')

    @app.get('/admin/backups/<filename>/download')
    @login_required
    def admin_backup_download(filename):
        return send_file(BytesIO(backup_content(filename)), mimetype='application/vnd.sqlite3',
                         as_attachment=True, download_name=filename, max_age=0)

    @app.post('/admin/backups/<filename>/restore')
    @login_required
    def admin_backup_restore(filename):
        content = backup_content(filename)
        try:
            texts = read_database_texts(content, [key for key, _, _ in RACI_FIELDS])
        except ValueError as error:
            return render_template('admin_editor.html', fields=RACI_FIELDS, texts=raci_texts(),
                                   db_version=database_version(), import_error=str(error),
                                   db_file=database_file_info(), backups=backup_history(),
                                   backup_limit=BACKUP_LIMIT), 400
        with database_change('before-restore') as db:
            db.executemany('UPDATE texts SET value=? WHERE key=?', [(value, key) for key, value in texts.items()])
        return redirect(url_for('admin_editor', restored='1'))

    @app.post('/admin/import')
    @login_required
    def admin_import():
        upload = request.files.get('database')
        try:
            if upload is None or not upload.filename:
                raise ValueError('Выберите файл базы SQLite.')
            texts = read_import(upload, [key for key, _, _ in RACI_FIELDS])
        except ValueError as error:
            return render_template('admin_editor.html', fields=RACI_FIELDS, texts=raci_texts(),
                                   db_version=database_version(), import_error=str(error),
                                   db_file=database_file_info(), backups=backup_history(),
                                   backup_limit=BACKUP_LIMIT), 400
        with database_change('before-import') as db:
            db.executemany('UPDATE texts SET value=? WHERE key=?', [(value, key) for key, value in texts.items()])
        return redirect(url_for('admin_editor', imported='1'))

    @app.post('/admin/logout')
    def admin_logout():
        session.clear()
        return redirect(url_for('admin_login'))

    @app.cli.command('set-admin')
    @click.option('--username', prompt='Логин администратора')
    @click.password_option(confirmation_prompt=True, prompt='Пароль (не менее 12 символов)')
    def set_admin_command(username, password):
        """Создать или обновить единственную учётную запись администратора."""
        try:
            set_admin(username, password)
        except (ValueError, BackupError) as error:
            raise click.ClickException(str(error)) from error
        click.echo('Учётная запись сохранена. Предыдущие сессии входа отозваны.')
