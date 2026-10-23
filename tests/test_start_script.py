"""Проверки обновления, повторов после ошибок и установки systemd без Docker/Git remote."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


class StartScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project space %'
        source = Path(__file__).resolve().parents[1]
        shutil.copytree(source / 'scripts', self.project / 'scripts')
        self.script = self.project / 'scripts/build-and-run.sh'
        self.state = self.project / '.git/feedback-templater-update'
        self.state.mkdir(parents=True)
        self.head = self.root / 'head'
        self.head.write_text('1111111')
        self.log = self.root / 'calls'
        self.config = self.root / 'config'
        bins = self.root / 'bin'
        bins.mkdir()
        mock = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
name = pathlib.Path(sys.argv[0]).name
root = pathlib.Path(os.environ['CHECK_ROOT'])
with (root/'calls').open('a') as out:
    out.write(json.dumps([name, args, os.getcwd(), os.environ.get('APP_COMMIT')])+'\n')
if name == 'git':
    if args[:2] == ['rev-parse', '--git-path']:
        print(str(pathlib.Path.cwd()/'.git/feedback-templater-update'))
    elif args[0] == 'rev-parse':
        print((root/'head').read_text())
    elif args[0] == 'pull':
        if os.environ.get('PULL_FAIL') == '1': sys.exit(1)
        if os.environ.get('CHANGE') == '1' and (root/'head').read_text() == '1111111':
            (root/'head').write_text('2222222')
            if os.environ.get('SELF_UPDATE') == '1':
                with pathlib.Path('scripts/build-and-run.sh').open('a') as out: out.write('\n# updated\n')
elif name == 'docker':
    if args[0] == 'build' and os.environ.get('BUILD_FAIL') == '1': sys.exit(1)
    if args[:2] == ['compose', 'exec'] and os.environ.get('VERIFY_FAIL') == '1': sys.exit(1)
elif name == 'systemctl':
    if args == ['--user', 'show-environment'] and os.environ.get('SYSTEMD_FAIL') == '1': sys.exit(1)
'''
        for name in ['git', 'docker', 'systemctl']:
            path = bins / name
            path.write_text('#!' + sys.executable + '\n' + mock)
            path.chmod(0o755)
        self.env = {**os.environ, 'PATH': str(bins) + ':' + os.environ['PATH'],
                    'CHECK_ROOT': str(self.root), 'XDG_CONFIG_HOME': str(self.config)}
        self.env.pop('FEEDBACK_TEMPLATER_LOCK_HELD', None)

    def run_script(self, *args, **env):
        self.log.unlink(missing_ok=True)
        result = subprocess.run(['bash', str(self.script), *args], cwd=self.root,
                                env={**self.env, **env}, capture_output=True, text=True, timeout=15)
        self.calls = [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []
        return result

    def docker_calls(self):
        return [call for call in self.calls if call[0] == 'docker']

    def test_no_change_skips_docker(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.docker_calls())
        self.assertTrue(all(call[2] == str(self.project) for call in self.calls))

    def test_changed_commit_builds_and_clears_pending(self):
        result = self.run_script(CHANGE='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        builds = [c for c in self.docker_calls() if c[1][0] == 'build']
        self.assertEqual(len(builds), 1)
        self.assertIn('APP_COMMIT=2222222', builds[0][1])
        self.assertEqual(builds[0][3], '2222222')
        self.assertFalse((self.state/'pending').exists())
        self.assertEqual(self.run_script().returncode, 0)
        self.assertFalse(self.docker_calls())

    def test_force_initial_build_without_new_commits(self):
        result = self.run_script('--force')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c[1][0] == 'build' for c in self.docker_calls()))

    def test_failed_build_is_retried_without_new_commits(self):
        self.assertNotEqual(self.run_script(CHANGE='1', BUILD_FAIL='1').returncode, 0)
        self.assertTrue((self.state/'pending').exists())
        self.assertFalse(any(c[1][:2] == ['compose','up'] for c in self.docker_calls()))
        self.assertEqual(self.run_script().returncode, 0)
        self.assertTrue(any(c[1][0] == 'build' for c in self.docker_calls()))
        self.assertFalse((self.state/'pending').exists())

    def test_failed_version_check_keeps_pending(self):
        self.assertNotEqual(self.run_script(CHANGE='1', VERIFY_FAIL='1').returncode, 0)
        self.assertTrue((self.state/'pending').exists())
        self.assertEqual(self.run_script().returncode, 0)
        self.assertFalse((self.state/'pending').exists())

    def test_pull_failure_skips_docker(self):
        self.assertNotEqual(self.run_script(PULL_FAIL='1').returncode, 0)
        self.assertFalse(self.docker_calls())

    def test_self_update_keeps_pending_and_lock(self):
        result = self.run_script(CHANGE='1', SELF_UPDATE='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(c[0]=='git' and c[1][0]=='pull' for c in self.calls), 2)
        self.assertEqual(sum(c[1][0]=='build' for c in self.docker_calls()), 1)
        self.assertFalse((self.state/'pending').exists())

    def test_concurrent_run_skips_pull(self):
        with (self.state/'update.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_script(CHANGE='1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(c[0]=='git' and c[1][0]=='pull' for c in self.calls))
        self.assertFalse(self.docker_calls())

    def test_install_systemd_for_actual_path(self):
        result = self.run_script('--install-systemd')
        self.assertEqual(result.returncode, 0, result.stderr)
        directory = self.config/'systemd/user'
        timer = (directory/'feedback-templater-update.timer').read_text()
        override = (directory/'feedback-templater-update.service.d/10-project.conf').read_text()
        self.assertIn('OnUnitInactiveSec=30s', timer)
        self.assertIn(str(self.project).replace('%','%%'), override)
        self.assertEqual([c[1] for c in self.calls], [
            ['--user','show-environment'], ['--user','daemon-reload'],
            ['--user','enable','--now','feedback-templater-update.timer']])

    def test_unavailable_systemd_writes_nothing(self):
        self.assertNotEqual(self.run_script('--install-systemd', SYSTEMD_FAIL='1').returncode, 0)
        self.assertFalse(self.config.exists())

    def test_help_has_no_effects(self):
        self.assertEqual(self.run_script('--help').returncode, 0)
        self.assertFalse(self.calls)
        self.assertFalse(self.run_script('--bad').returncode == 0)
