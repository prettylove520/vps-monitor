"""v0.5: one-click update from the workbench (web side and the root update.sh)."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import test_app  # noqa: F401
from test_v04 import AppCase
from store import db, set_setting
import updater
import updates

ROOT = Path(__file__).resolve().parent.parent
LATEST = {'version': '99.0.0', 'url': 'https://github.com/x/y/blob/main/CHANGELOG.md'}


class FetchLatestTests(unittest.TestCase):
    def fetch(self, text):
        class Response:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                return False

            def read(s, n):
                return text.encode()

        with patch('updates.urllib.request.urlopen', return_value=Response()) as opened:
            return updates.fetch_latest(), opened

    def test_reads_version_from_main(self):
        (version, url), opened = self.fetch("VERSION = '1.2.3'\n")
        self.assertEqual(version, '1.2.3')
        self.assertIn('raw.githubusercontent.com/' + updates.REPO + '/main/version.py', opened.call_args[0][0].full_url)
        self.assertTrue(url.startswith('https://github.com/'))

    def test_garbage_is_rejected(self):
        with self.assertRaises(ValueError):
            self.fetch('<html>not found</html>')

    def test_manual_check_is_rate_limited(self):
        calls = []

        def fake():
            calls.append(1)
            return '99.0.0', 'https://github.com/x'

        updates.check_now(now=10**7, fetch=fake)
        updates.check_now(now=10**7 + 10, fetch=fake)
        self.assertEqual(len(calls), 1)
        updates.check_now(now=10**7 + 90, fetch=fake)
        self.assertEqual(len(calls), 2)

    def test_manual_check_reports_what_happened(self):
        ok = lambda: ('1.0.0', 'https://github.com/x')
        self.assertEqual(updates.check_now(now=10**8, fetch=ok), 'ok')
        self.assertEqual(updates.check_now(now=10**8 + 5, fetch=ok), 'throttled')
        self.assertEqual(updates.check_now(now=10**8 + 100, fetch=lambda: 1 / 0), 'failed')
        with patch.dict(os.environ, {'UPDATE_CHECK': '0'}):
            self.assertEqual(updates.check_now(now=10**8 + 500, fetch=ok), 'disabled')


class UpdateApiTests(AppCase):
    def setUp(self):
        super().setUp()
        self.dir = Path(tempfile.mkdtemp())
        self.patches = [patch.object(updater, 'REQUEST', self.dir / 'update-request'),
                        patch.object(updater, 'STATUS', self.dir / 'update-status.json'),
                        patch.object(updater, 'DATA', self.dir),
                        patch.dict(os.environ, {'UPDATE_APPLY': '1'})]
        for p in self.patches:
            p.start()
        set_setting('update_latest', json.dumps(LATEST))
        self.login()
        with db() as c:
            c.execute('DELETE FROM checkout_preflights')

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def apply(self):
        return self.req('/api/update/apply', data={})

    def test_requires_login_and_csrf(self):
        anonymous = self.app.test_client() if hasattr(self, 'app') else None
        if anonymous is not None:
            self.assertEqual(anonymous.get('/api/update').status_code, 401)
        r = self.client.post('/api/update/apply', json={'password': 'x'})
        self.assertEqual(r.status_code, 403)  # no CSRF token
        self.assertFalse(updater.REQUEST.exists())

    def test_state(self):
        s = self.client.get('/api/update').json
        self.assertEqual((s['latest']['version'], s['can_apply'], s['progress'], s['armed']), ('99.0.0', True, None, 0))

    def test_apply_drops_request_file_once(self):
        r = self.apply()
        self.assertEqual(r.status_code, 200, r.json)
        self.assertTrue(updater.REQUEST.exists())
        self.assertEqual(r.json['progress']['state'], 'queued')
        self.assertIn('更新正在进行中', self.apply().json['error'])

    def test_not_available_without_flag_or_without_new_version(self):
        with patch.dict(os.environ, {'UPDATE_APPLY': '0'}):
            self.assertFalse(self.client.get('/api/update').json['can_apply'])
            self.assertEqual(self.apply().status_code, 400)
        set_setting('update_latest', json.dumps({'version': '0.0.1', 'url': 'https://github.com/x'}))
        self.assertIn('最新版本', self.apply().json['error'])
        self.assertFalse(updater.REQUEST.exists())

    def test_refused_while_an_order_is_running(self):
        with db() as c:
            tid = c.execute("INSERT INTO tasks(config,created) VALUES ('{}',0)").lastrowid
            c.execute("INSERT INTO checkout_preflights(task_id,task_revision,mode,state,created) VALUES (?,?,?,?,?)",
                      (tid, 1, 'purchase', 'running', time.time()))
        self.assertIn('自动下单', self.apply().json['error'])
        self.assertFalse(updater.REQUEST.exists())

    def test_status_file_and_stale_running(self):
        updater.STATUS.write_text(json.dumps({'state': 'running', 'message': 'x', 'time': time.time()}))
        self.assertEqual(updater.status()['state'], 'running')
        self.assertIn('更新正在进行中', self.apply().json['error'])
        updater.STATUS.write_text(json.dumps({'state': 'running', 'message': 'x', 'time': time.time() - 3600}))
        self.assertEqual(updater.status()['state'], 'failed')
        updater.STATUS.write_text('not json')
        self.assertIsNone(updater.status())

    def test_check_endpoint_returns_state(self):
        set_setting('update_checked', '0')
        with patch('updates.fetch_latest', return_value=('100.0.0', 'https://github.com/x')):
            s = self.req('/api/update/check', data={}).json
        self.assertEqual((s['latest']['version'], s['result']), ('100.0.0', 'ok'))

    def test_dashboard_tells_the_page_whether_it_can_apply(self):
        self.assertTrue(self.client.get('/api/dashboard').json['update_can_apply'])


@unittest.skipUnless(shutil.which('bash') and shutil.which('tar'), 'needs bash')
class UpdateScriptTests(unittest.TestCase):
    """Runs the real deploy/update.sh against a throw-away directory tree."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app, self.data, self.backups = self.tmp / 'app', self.tmp / 'data', self.tmp / 'backups'
        self.units = self.tmp / 'units'
        self.units.mkdir()
        self.data.mkdir()
        self.copy_project(self.app, '0.0.1')
        self.src = self.tmp / 'new'
        self.copy_project(self.src, '9.9.9')
        con = sqlite3.connect(self.data / 'watch.db')
        con.execute('CREATE TABLE t(x)')
        con.execute("INSERT INTO t VALUES ('before')")
        con.commit()
        con.close()
        (self.tmp / 'env').write_text('WATCH_DATA=x\nUPDATE_REPO=owner/repo\n')
        (self.data / 'update-request').write_text('{}')
        self.log = self.tmp / 'calls.log'

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def copy_project(self, dest, version):
        dest.mkdir(parents=True, exist_ok=True)
        for f in list(ROOT.glob('*.py')) + [ROOT / 'requirements.txt', ROOT / 'install.sh']:
            shutil.copy(f, dest / f.name)
        for sub in ('static', 'merchants', 'deploy'):
            shutil.copytree(ROOT / sub, dest / sub, ignore=shutil.ignore_patterns('__pycache__'), dirs_exist_ok=True)
        (dest / 'version.py').write_text("VERSION = '%s'\n" % version)

    def run_script(self, overrides='', env=None):
        script = f'''
source "{ROOT}/deploy/update.sh"
fetch_source() {{ cp -r "{self.src}" "$2/src"; }}
restart_services() {{ echo restart >> "{self.log}"; }}
pip_install() {{ echo pip >> "{self.log}"; }}
healthy() {{ true; }}
sleep() {{ :; }}
{overrides}
main
echo "exit=$?"
'''
        full = dict(os.environ, APP=str(self.app), DATA=str(self.data), BACKUPS=str(self.backups),
                    ENV_FILE=str(self.tmp / 'env'), SYSTEMCTL='true', PIP='true', PYTHON=sys.executable,
                    OWNER=str(os.getuid()), UNIT_DIR=str(self.units))
        full.update(env or {})
        out = subprocess.run(['bash', '-c', script], capture_output=True, text=True, env=full, timeout=120)
        return out.stdout + out.stderr

    def status(self):
        return json.loads((self.data / 'update-status.json').read_text())

    def version(self):
        return (self.app / 'version.py').read_text()

    def test_successful_update(self):
        out = self.run_script()
        self.assertIn('exit=0', out, out)
        self.assertEqual((self.status()['state'], self.status()['percent']), ('done', 100))
        self.assertIn('9.9.9', self.version())
        self.assertFalse((self.data / 'update-request').exists())
        self.assertEqual(self.log.read_text().split(), ['pip', 'restart'])
        backup = next(self.backups.iterdir())
        self.assertTrue((backup / 'app.tar.gz').exists() and (backup / 'watch.db').exists())
        self.assertTrue((self.units / 'vps-watch-update.path').exists())
        self.assertTrue(os.access(self.app / 'update.sh', os.X_OK))

    def test_failed_start_rolls_back_code_and_data(self):
        out = self.run_script('''
restart_services() { sqlite3 "$DATA/watch.db" "INSERT INTO t VALUES ('after')" 2>/dev/null || "$PYTHON" -c "import sqlite3;c=sqlite3.connect('$DATA/watch.db');c.execute(\\"INSERT INTO t VALUES ('after')\\");c.commit()"; }
healthy() { [ "$1" = "0.0.1" ]; }
''')
        self.assertIn('exit=1', out, out)
        self.assertEqual(self.status()['state'], 'rolled_back')
        self.assertIn('0.0.1', self.version())
        rows = [r[0] for r in sqlite3.connect(self.data / 'watch.db').execute('SELECT x FROM t')]
        self.assertEqual(rows, ['before'])

    def test_rollback_failure_is_reported_with_backup_location(self):
        self.run_script('healthy() { false; }')
        s = self.status()
        self.assertEqual(s['state'], 'failed')
        self.assertIn(str(self.backups), s['message'])

    def test_already_current_changes_nothing(self):
        (self.src / 'version.py').write_text("VERSION = '0.0.1'\n")
        out = self.run_script()
        self.assertIn('exit=0', out, out)
        self.assertEqual(self.status()['state'], 'done')
        self.assertFalse(self.backups.exists())
        self.assertFalse(self.log.exists())

    def test_older_version_is_never_installed(self):
        (self.src / 'version.py').write_text("VERSION = '0.0.0'\n")
        self.run_script()
        self.assertIn('0.0.1', self.version())

    def test_symlinks_and_missing_files_are_refused(self):
        os.symlink('/etc/passwd', self.src / 'evil.py')
        self.run_script()
        self.assertEqual(self.status()['state'], 'failed')
        self.assertIn('符号链接', self.status()['message'])
        self.assertIn('0.0.1', self.version())
        os.remove(self.src / 'evil.py')
        os.remove(self.src / 'deploy' / 'update.sh')
        self.run_script()
        self.assertIn('update.sh', self.status()['message'])

    def test_syntax_error_in_new_version_is_refused(self):
        (self.src / 'app.py').write_text('def broken(:\n')
        self.run_script()
        self.assertEqual(self.status()['state'], 'failed')
        self.assertIn('0.0.1', self.version())
        self.assertFalse(self.log.exists())

    def test_bad_repo_name_is_refused_without_downloading(self):
        (self.tmp / 'env').write_text('UPDATE_REPO=a/b;rm -rf /\n')
        self.run_script('fetch_source() { echo called >> "%s"; }' % self.log)
        self.assertEqual(self.status()['state'], 'failed')
        self.assertFalse(self.log.exists())

    def test_download_failure_keeps_everything(self):
        self.run_script('fetch_source() { return 1; }')
        self.assertEqual(self.status()['state'], 'failed')
        self.assertIn('0.0.1', self.version())

    def test_only_five_backups_are_kept(self):
        self.backups.mkdir()
        for i in range(8):
            (self.backups / ('2020010%d-000000-v0' % i)).mkdir()
        self.run_script()
        self.assertEqual(len(list(self.backups.iterdir())), 5)

    def test_version_comparison(self):
        out = subprocess.run(['bash', '-c', f'source "{ROOT}/deploy/update.sh"; version_newer 0.10.0 0.9.9 && echo yes; '
                              'version_newer 0.4.1 0.4.1 || echo no'], capture_output=True, text=True).stdout
        self.assertEqual(out.split(), ['yes', 'no'])

    def test_script_and_units_are_consistent(self):
        path = (ROOT / 'deploy/vps-watch-update.path').read_text()
        service = (ROOT / 'deploy/vps-watch-update.service').read_text()
        self.assertIn('PathExists=/var/lib/vps-watch/update-request', path)
        self.assertIn('ExecStart=/opt/vps-watch/update.sh', service)
        self.assertEqual(str(updater.REQUEST.name), 'update-request')
        self.assertEqual(subprocess.run(['bash', '-n', str(ROOT / 'deploy/update.sh')]).returncode, 0)


class NoExampleTaskTests(unittest.TestCase):
    def test_seed_creates_no_task_so_upgrades_cannot_bring_one_back(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        for _ in range(2):
            subprocess.run([sys.executable, str(ROOT / 'manage.py'), 'seed'], check=True, capture_output=True,
                           env=dict(os.environ, WATCH_DATA=str(d)), cwd=ROOT)
        self.assertEqual(sqlite3.connect(d / 'watch.db').execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 0)


class AccountPageTests(AppCase):
    def test_dashboard_says_whether_an_account_is_saved(self):
        import accounts
        self.login()
        with db() as c:
            c.execute('DELETE FROM merchant_accounts')
        self.assertFalse(self.client.get('/api/dashboard').json['account_saved'])
        accounts.save_credentials({'email': 'account@example.com', 'password': 'private-password', 'auto_relogin': True})
        self.assertTrue(self.client.get('/api/dashboard').json['account_saved'])


class HiddenTemplateTests(AppCase):
    def test_product_templates_are_not_offered_but_existing_tasks_keep_working(self):
        self.login()
        presets = self.client.get('/api/dashboard').json['presets']
        self.assertEqual(sorted(presets), ['generic', 'jsonapi', 'vmiss', 'whmcs'])
        import adapters
        self.assertEqual(self.req('/api/tasks', data=adapters.PRESETS['bero']).status_code, 201)


if __name__ == '__main__':
    unittest.main()
