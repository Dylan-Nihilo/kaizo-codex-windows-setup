"""Offline regression check: temporary files and fake child processes only."""
import importlib.util
import ast
import contextlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

sys.dont_write_bytecode = True


def check_windows_permissions(setup, root):
    denied = PermissionError(13, 'Access is denied', r'C:\WindowsApps\winget.exe')
    denied.winerror = 5
    app = {'app': 'fixture-app', 'binary': 'fixture-cli'}
    with patch.object(setup, 'applications', side_effect=[[], [], [app]]), \
         patch.object(setup.shutil, 'which', return_value='winget.exe'), \
         patch.object(setup.subprocess, 'Popen', side_effect=denied), \
         patch.object(setup, 'download') as download, patch.object(setup, 'ps'):
        assert setup.prepare_codex(root, 'x64', False, lambda _: None) == app
        assert download.call_count == 1
    # Execute the actual final exception handler; real app startup and real credentials are replaced.
    tree = ast.parse(Path(setup.__file__).read_text())
    entry = compile(ast.Module(body=[tree.body[-1]], type_ignores=[]), 'setup.py', 'exec')
    def fail(_):
        raise denied
    output = io.StringIO()
    with patch.object(setup, 'ROOT', root), patch.object(sys, 'argv', ['setup.py']), contextlib.redirect_stdout(output):
        try:
            exec(entry, dict(vars(setup), __name__='__main__', main=fail, close_log=lambda: None))
        except SystemExit as stopped:
            assert stopped.code == 1
    assert 'WinError 5' in output.getvalue() and 'winget.exe' in output.getvalue()
    assert '代码位置' in output.getvalue() and (root / 'Last-error.txt').exists()
    secret_error = PermissionError(13, 'Access is denied', 'https://srt:fixture-secret@localhost/sk-fixture-secret-key')
    summary = setup.describe_error(secret_error)
    assert 'fixture-secret' not in summary and '[KEY]' in summary and '[CREDENTIALS]' in summary

    class FakeRegistry:
        HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = 'HKCU', 'HKLM'
        KEY_QUERY_VALUE, KEY_SET_VALUE = 1, 2
        def __init__(self):
            self.values = {'HTTP_PROXY': ('http://srt:fixture@localhost:64321', 1)}
            self.accesses = []
        def OpenKey(self, hive, path, reserved=0, access=0x20019):
            # A restricted fixture grants only query/set, not broad key-management permissions.
            assert access in (1, 2, 3), 'unnecessary registry rights requested'
            self.accesses.append(access)
            data = self.values if hive == 'HKCU' and path == 'Environment' else {'ProxyEnable': (0, 4)} if path.endswith('Internet Settings') else {}
            return contextlib.nullcontext((data, access))
        CreateKeyEx = OpenKey
        def QueryInfoKey(self, handle):
            assert handle[1] & 1
            return (0, len(handle[0]), 0)
        def EnumValue(self, handle, index):
            assert handle[1] & 1
            name, (value, kind) = list(handle[0].items())[index]
            return name, value, kind
        def QueryValueEx(self, handle, name):
            if not handle[1] & 1:
                raise PermissionError(13, 'Query access denied')
            return handle[0][name]
        def DeleteValue(self, handle, name):
            assert handle[1] & 2
            del handle[0][name]
        def SetValueEx(self, handle, name, reserved, kind, value):
            assert handle[1] & 2
            handle[0][name] = value, kind

    registry = FakeRegistry()
    stale = registry.values['HTTP_PROXY'][0]
    with patch.dict(sys.modules, {'winreg': registry}), patch.object(setup, 'private'), \
         patch.object(setup, 'ps', return_value=str(root / 'documents')), \
         patch.object(setup, 'is_dead', side_effect=lambda value: value == stale), \
         patch.object(setup, 'direct_probe', return_value=True), patch.object(setup, 'broadcast_environment'), \
         patch.dict(os.environ, {'HTTP_PROXY': stale}):
        backup = setup.Backup(root / 'registry-backup')
        setup.proxy_preflight(root, root / 'proxy-codex', backup, lambda _: None)
        assert registry.values == {} and 3 in registry.accesses and 'HTTP_PROXY' not in os.environ
        backup.restore()
        assert registry.values == {'HTTP_PROXY': (stale, 1)} and registry.accesses[-1] == 2

    # A filename-less registry error still identifies the affected key.
    with patch.object(registry, 'OpenKey', side_effect=PermissionError(13, 'Access denied')):
        try:
            with setup.registry_key(registry, registry.HKEY_CURRENT_USER, 'Environment', 1):
                pass
            raise AssertionError('registry failure swallowed')
        except PermissionError as error:
            assert 'HKCU\\Environment' in setup.describe_error(error)

    # A failed file replacement is visible after the progress screen and rollback finish.
    blocked = root / 'locked-config.toml'
    file_error = PermissionError(13, 'Access is denied', str(blocked))
    file_error.winerror = 32
    ui = setup.Dashboard()
    ui.animated = False
    with contextlib.redirect_stdout(io.StringIO()):
        with ui.step(0, 'fixture successful step'):
            pass
        try:
            with ui.step(1, 'fixture denied write'), patch.object(setup.os, 'replace', side_effect=file_error):
                setup.atomic(blocked, 'sk-never-log-file-contents')
        except PermissionError as error:
            setup.report_failure(error, '02 fixture denied write', root)
        else:
            raise AssertionError('file denial not exercised')


def check_logging(setup, root):
    # A denied log directory must fall back before any configuration changes.
    blocked = root / 'not-a-directory'
    blocked.write_text('fixture')
    with patch.object(setup, 'ROOT', root), patch.dict(os.environ, {'KAIZO_SETUP_LOG': str(blocked / 'setup.log')}), \
         contextlib.redirect_stdout(io.StringIO()):
        setup.init_log('check')
        assert setup.LOG_PATH.parent == root / 'logs'
        setup.close_log()
    log_path = root / 'fixture-setup.log'
    log_path.write_text('bootstrap fixture\n')
    with patch.dict(os.environ, {'KAIZO_SETUP_LOG': str(log_path)}), contextlib.redirect_stdout(io.StringIO()):
        setup.init_log('check')
    setup.LOG.info('secrets sk-fixture-secret-key socks5://user:fixture-proxy-password@localhost:9 Bearer fixture-bearer-token refresh_token="fixture-refresh-token" api_key=fixture-apikey-value')
    setup.run([sys.executable, '-c', 'print("fixture-process-output")'])
    try:
        setup.run([sys.executable, '-c', 'import sys; sys.stderr.write("0x80070005 sk-process-secret"); sys.exit(7)'])
    except setup.Failure as error:
        assert '0x80070005' in str(error) and '7' in str(error)
        setup.LOG.error('fixture subprocess failure %s', error)
    else:
        raise AssertionError('nonzero child exit not exercised')
    return log_path


def check_public_setup(setup, root):
    fixture_key = 'sk-public-input-fixture-never-sent'
    package = root / 'public-package'
    package.mkdir()
    local = root / 'public-localappdata'
    with patch.object(setup, 'ROOT', package), patch.dict(os.environ, {'LOCALAPPDATA': str(local), 'KAIZO_API_KEY': ''}), \
         contextlib.redirect_stdout(io.StringIO()):
        os.environ.pop('KAIZO_API_KEY')
        with patch.object(setup.getpass, 'getpass', return_value=fixture_key) as prompt:
            assert setup.load_provider() == fixture_key
            assert prompt.call_count == 1
        saved = local / 'KAIZO-Setup/provider.json'
        assert json.loads(saved.read_text())['key'] == fixture_key
        assert not (package / 'provider.json').exists()
        with patch.object(setup.getpass, 'getpass', side_effect=AssertionError('upgrade prompted again')):
            assert setup.load_provider() == fixture_key
        saved.unlink()
        with patch.object(setup.getpass, 'getpass', return_value='invalid'):
            try:
                setup.load_provider()
                raise AssertionError('invalid key saved')
            except setup.Failure:
                assert not saved.exists()
        with patch.object(setup.getpass, 'getpass', side_effect=setup.getpass.GetPassWarning):
            try:
                setup.load_provider()
                raise AssertionError('echoed password input accepted')
            except setup.Failure:
                assert not saved.exists()
        (package / 'provider.json').write_text(json.dumps({'url': 'https://kaizo.top', 'key': fixture_key}))
        with patch.object(setup.getpass, 'getpass', side_effect=AssertionError('legacy key not imported')):
            assert setup.load_provider() == fixture_key
        assert saved.exists()
        payload = b'fixture installer bytes, never executed'
        asset = {'url': 'https://example.invalid/fixture.msi', 'file': 'fixture.msi', 'sha256': setup.hashlib.sha256(payload).hexdigest()}
        (package / 'assets').mkdir()
        (package / 'assets/manifest.json').write_text(json.dumps({'x64': {'cc': asset}}))
        app = {'app': 'fixture-app', 'version': '3.20.2'}
        def fetch(_, target, detail):
            target.write_bytes(payload)
        with patch.dict(os.environ, {'SystemRoot': str(root / 'fake-windows')}), \
             patch.object(setup, 'download', side_effect=fetch) as download, patch.object(setup, 'run') as install:
            for _ in range(2):
                with patch.object(setup, 'find_cc', side_effect=[[], [app]]):
                    assert setup.prepare_cc('x64', False, lambda _: None) == 'fixture-app'
            assert download.call_count == 1 and install.call_count == 2
        (local / 'KAIZO-Setup/Downloads/fixture.msi').write_bytes(b'corrupted cache')
        with patch.object(setup, 'find_cc', return_value=[]), \
             patch.object(setup, 'download', side_effect=lambda _, target, detail: target.write_bytes(b'wrong download')), \
             patch.object(setup, 'run') as install:
            try:
                setup.prepare_cc('x64', False, lambda _: None)
                raise AssertionError('unverified installer executed')
            except setup.Failure:
                install.assert_not_called()


def check_no_codex_start(setup, root, codex, cc, key):
    blocked = r'C:\Program Files\WindowsApps\OpenAI.Codex_26.901.6511.0_x64__2p2nqsd0c76g0\app\resources\codex.exe'
    denied = PermissionError(13, 'Access is denied', blocked)
    denied.winerror = 5
    output = io.StringIO()
    with patch.object(setup, 'paths_and_checks', return_value=(root, codex, cc, 'x64')), \
         patch.object(setup, 'load_provider', return_value=key), patch.object(setup, 'private'), \
         patch.object(setup, 'wait_closed'), patch.object(setup, 'proxy_preflight'), \
         patch.object(setup, 'prepare_codex', return_value={'app': blocked, 'binary': blocked}), \
         patch.object(setup, 'prepare_cc', return_value='fixture-cc.exe'), \
         patch.object(setup, 'processes', return_value=[]), patch.object(setup, 'init_log'), \
         patch('builtins.input', return_value=''), patch.object(setup, 'start_process', side_effect=denied) as launch, \
         contextlib.redirect_stdout(output):
        setup.main('repair')
        launch.assert_not_called()
    setup.verify_config(setup.tomllib.loads((codex / 'config.toml').read_text()), key)
    assert '未进行联网验证' in output.getvalue()


def check_cc_form_fields(text, setup):
    # CC Switch 3.20.2 providerConfigUtils.ts: the form scans bare keys and literal section names.
    # This checks the serialized database value, separately from the TOML semantic checks.
    lines = text.splitlines()
    section = None
    found = {}
    section_pattern = setup.re.compile(r'^\s*\[([^\]\r\n]+)\]\s*$')
    value_pattern = setup.re.compile(r'^\s*(model_provider|model|base_url|wire_api)\s*=\s*"([^"\r\n]+)"\s*$')
    for line in lines:
        header = section_pattern.match(line)
        if header:
            section = header[1]
            continue
        value = value_pattern.match(line)
        if value:
            found[(section, value[1])] = value[2]
    assert found.get((None, 'model_provider')) == 'custom', 'CC Switch cannot identify the selected provider'
    assert found.get((None, 'model')) == setup.MODEL, 'CC Switch model field is empty'
    assert found.get(('model_providers.custom', 'base_url')) == setup.BASE, 'CC Switch API request address is empty'
    assert found.get(('model_providers.custom', 'wire_api')) == 'responses'


def check():
    spec = importlib.util.spec_from_file_location('kaizo_setup', Path(__file__).with_name('setup.py'))
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    dummy_key = 'sk-offline-fixture-key-never-sent'
    stale = 'http://srt:offline@localhost:64321'
    fresh = 'http://srt:other@localhost:54321'
    profile_before = '$env:HTTP_PROXY = "' + stale + '"\nWrite-Output "keep"\n'
    profile = setup.profile_cleanup(profile_before, {stale})
    assert profile.startswith(profile_before) and setup.profile_cleanup(profile, {stale}) == profile
    assert '-ccontains' in profile and fresh not in profile
    dotenv = 'KEEP=value\nexport HTTP_PROXY="' + stale + '" # old\nhttps_proxy=' + fresh + '\n'
    cleaned = setup.dotenv_cleanup(dotenv, {stale})
    assert stale not in cleaned and fresh in cleaned and 'KEEP=value' in cleaned
    with patch.object(setup.socket, 'create_connection', side_effect=ConnectionRefusedError):
        assert setup.is_dead(stale) and not setup.is_dead('http://remote.example:8080')
    with patch.object(setup.socket, 'create_connection', side_effect=TimeoutError):
        assert not setup.is_dead(stale)
    assert setup.proxy_url('http://[::1]:1234') == ('::1', 1234)
    assert setup.proxy_url('http://localhost:invalid') is None
    with tempfile.TemporaryDirectory(prefix='kaizo-offline-check-') as temporary, contextlib.ExitStack() as cleanup:
        root = Path(temporary).resolve()
        cleanup.callback(setup.close_log)
        log_path = check_logging(setup, root)
        check_public_setup(setup, root)
        codex, cc = root / 'fake-codex', root / 'fake-cc'
        codex.mkdir(); cc.mkdir()
        db_path = cc / 'cc-switch.db'
        with sqlite3.connect(db_path) as db:
            db.executescript("""
            PRAGMA user_version=18;
            CREATE TABLE providers(id TEXT NOT NULL,app_type TEXT NOT NULL,name TEXT NOT NULL,
                settings_config TEXT NOT NULL,website_url TEXT,category TEXT,created_at INTEGER,
                sort_index INTEGER,notes TEXT,icon TEXT,icon_color TEXT,meta TEXT NOT NULL DEFAULT '{}',
                is_current BOOLEAN NOT NULL DEFAULT 0,in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
                PRIMARY KEY(id,app_type));
            CREATE TABLE prompts(id TEXT NOT NULL,app_type TEXT NOT NULL,name TEXT NOT NULL,
                content TEXT NOT NULL,description TEXT,enabled BOOLEAN NOT NULL DEFAULT 1,
                created_at INTEGER,updated_at INTEGER,PRIMARY KEY(id,app_type));
            CREATE TABLE proxy_config(app_type TEXT PRIMARY KEY,enabled INTEGER DEFAULT 0,
                auto_failover_enabled INTEGER DEFAULT 0);
            INSERT INTO proxy_config(app_type) VALUES('codex');
            INSERT INTO providers(id,app_type,name,settings_config,is_current)
                VALUES('other','claude','Unrelated','{}',1),('old','codex','Old','{}',1);
            INSERT INTO prompts(id,app_type,name,content) VALUES('old','codex','Old','Old persona');
            """)
        db.close()
        original = '# keep in backup\nmodel = "old"\nsandbox_mode = "read-only"\n[features]\nkeep_feature = true\n'
        (codex / 'config.toml').write_text(original)
        (codex / 'AGENTS.md').write_text('Old guidelines')
        (codex / 'AGENTS.override.md').write_text('Old override')
        (cc / 'settings.json').write_text('{"unrelated": "keep"}')
        originals = {path: path.read_bytes() for path in (codex / 'config.toml', codex / 'AGENTS.md', codex / 'AGENTS.override.md', cc / 'settings.json')}
        backup = setup.Backup(root / 'backup')
        for path in (*originals, codex / 'auth.json'):
            backup.capture(path)
        backup.capture(db_path, database=True)
        agents = Path(__file__).with_name('AGENTS.md').read_text()
        assert all(word not in agents for word in ('N.O.V.A.', 'Dylan', 'Sir', '女性', 'female'))
        # Drive main through all steps with an inaccessible WindowsApps binary and forbid all launches.
        check_no_codex_start(setup, root, codex, cc, dummy_key)
        backup.restore()
        # Upgrade the quoted-key/quoted-section output produced by Windows v2.4.0.
        (codex / 'config.toml').write_text(
            '"model_provider" = "custom"\n"model" = "old"\n"sandbox_mode" = "read-only"\n'
            '["features"]\n"keep_feature" = true\n'
            '["model_providers"."custom"]\n"base_url" = "https://kaizo.top/v1"\n')
        with patch.object(setup, 'start_process', side_effect=AssertionError('configuration started a process')), patch.object(setup, 'private'):
            for _ in range(2):
                assert setup.configure(codex, cc, dummy_key, agents) == 'file'
                config = setup.tomllib.loads((codex / 'config.toml').read_text())
                setup.verify_config(config, dummy_key)
                assert config['sandbox_mode'] == 'read-only' and config['features']['keep_feature'] is True
                assert not (codex / 'AGENTS.override.md').exists()
                assert (codex / 'AGENTS.md').read_text() == agents
                assert json.loads((codex / 'auth.json').read_text()) == {'OPENAI_API_KEY': dummy_key}
                settings = json.loads((cc / 'settings.json').read_text())
                assert settings['preserveCodexOfficialAuthOnSwitch'] is True and settings['unrelated'] == 'keep'
                with sqlite3.connect(db_path) as db:
                    saved = db.execute("SELECT settings_config FROM providers WHERE app_type='codex' AND is_current=1").fetchall()
                    assert len(saved) == 1 and json.loads(saved[0][0])['auth']['OPENAI_API_KEY'] == dummy_key
                    assert json.loads(saved[0][0])['config'] == (codex / 'config.toml').read_text()
                    check_cc_form_fields(json.loads(saved[0][0])['config'], setup)
                    assert db.execute("SELECT is_current FROM providers WHERE app_type='claude'").fetchone() == (1,)
                    assert db.execute("SELECT content FROM prompts WHERE app_type='codex' AND enabled=1").fetchall() == [(agents,)]
                db.close()
        config['service_tier'] = 'fast'
        try:
            setup.verify_config(config, dummy_key)
            raise AssertionError('Fast override accepted')
        except setup.Failure:
            pass
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE proxy_config SET enabled=1 WHERE app_type='codex'")
        db.close()
        try:
            setup.check_db(db_path)
            raise AssertionError('proxy takeover accepted')
        except setup.Failure:
            pass
        backup.restore()
        assert all(path.read_bytes() == value for path, value in originals.items())
        assert not (codex / 'auth.json').exists()
        oauth = {'auth_mode': 'chatgpt', 'tokens': {'id_token': 'fixture-id', 'access_token': 'personal-access-token', 'refresh_token': 'personal-refresh-token'}}
        # Existing file credentials remain byte-for-byte intact; keyring/auto are never probed or overwritten.
        for store in ('file', 'keyring', 'auto'):
            backup.restore()
            personal_config = '# personal settings\nmodel_provider = "openai"\nmodel = "my-chosen-model"\ncli_auth_credentials_store = "' + store + '"\n'
            (codex / 'config.toml').write_text(personal_config)
            before = json.dumps(oauth, indent=4).encode() if store == 'file' else None
            if before:
                (codex / 'auth.json').write_bytes(before)
            for _ in range(2):
                with patch.object(setup, 'private'), patch.object(setup, 'start_process', side_effect=AssertionError('offline account preservation started a process')):
                    assert setup.configure(codex, cc, dummy_key, agents) == store
                assert ((codex / 'auth.json').read_bytes() if (codex / 'auth.json').exists() else None) == before
                with sqlite3.connect(db_path) as db:
                    rows = db.execute("SELECT id,category,settings_config FROM providers WHERE id IN (?,?)", (setup.PERSONAL_PROVIDER, setup.PROVIDER)).fetchall()
                db.close()
                cards = {row[0]: (row[1], json.loads(row[2])) for row in rows}
                assert cards[setup.PERSONAL_PROVIDER] == ('official', {'auth': {}, 'config': personal_config})
                assert cards[setup.PROVIDER][1]['auth'] == {'OPENAI_API_KEY': dummy_key}
                assert 'personal-refresh-token' not in json.dumps(cards[setup.PROVIDER])
            for selected in (setup.PERSONAL_PROVIDER, setup.PROVIDER, setup.PERSONAL_PROVIDER):
                selected_config = cards[selected][1]['config']
                (codex / 'config.toml').write_text(selected_config)
                current = setup.tomllib.loads((codex / 'config.toml').read_text())
                if selected == setup.PERSONAL_PROVIDER:
                    assert current['model_provider'] == 'openai' and current['model'] == 'my-chosen-model'
                    assert dummy_key not in selected_config
                else:
                    setup.verify_config(current, dummy_key, store)
                assert ((codex / 'auth.json').read_bytes() if (codex / 'auth.json').exists() else None) == before
        backup.restore()
        personal_key = 'sk-fictional-personal-official-key'
        (codex / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': personal_key}))
        before = (codex / 'auth.json').read_bytes()
        setup.configure(codex, cc, dummy_key, agents)
        assert (codex / 'auth.json').read_bytes() == before
        with sqlite3.connect(db_path) as db:
            personal = json.loads(db.execute("SELECT settings_config FROM providers WHERE id=?", (setup.PERSONAL_PROVIDER,)).fetchone()[0])
        db.close()
        assert personal['auth']['OPENAI_API_KEY'] == personal_key and dummy_key not in json.dumps(personal)
        backup.restore()
        (codex / 'config.toml').write_text('forced_login_method = "chatgpt"\n')
        setup.configure(codex, cc, dummy_key, agents)
        assert not (codex / 'auth.json').exists()
        assert setup.tomllib.loads((codex / 'config.toml').read_text())['forced_login_method'] == 'chatgpt'
        backup.restore()
        (codex / 'auth.json').write_text('{invalid json')
        try:
            setup.configure(codex, cc, dummy_key, agents)
            raise AssertionError('invalid auth overwritten')
        except json.JSONDecodeError:
            assert (codex / 'config.toml').read_text() == original
            assert (codex / 'auth.json').read_text() == '{invalid json'
        backup.restore()
        with sqlite3.connect(db_path) as db:
            db.execute("INSERT INTO providers(id,app_type,name,settings_config,category) VALUES(?,'codex','Existing','{}','third_party')", (setup.PERSONAL_PROVIDER,))
        db.close()
        try:
            setup.configure(codex, cc, dummy_key, agents)
            raise AssertionError('conflicting provider overwritten')
        except setup.Failure:
            assert (codex / 'config.toml').read_text() == original
        values = {'name.with.dots': '中文\nquoted "text"\t', 'switch': True, 'integer': 9, 'fraction': 1.5,
                  'date': setup.datetime.date(2026, 9, 9), 'time': setup.datetime.time(12, 34, 56),
                  'datetime': setup.datetime.datetime(2026, 9, 9, 12, 34, tzinfo=setup.datetime.timezone.utc),
                  'nested': {'quoted.key': {'empty': {}, 'mixed': [1, 'two', {'nested': [True, False]}]}},
                  'mcp_servers': {'fixture': {'command': 'fixture', 'args': ['--keep', r'C:\path with spaces\tool.exe']}}}
        assert setup.tomllib.loads(setup.toml_text(values)) == values
        refresh_backup = setup.Backup(root / 'refresh-backup')
        (codex / 'auth.json').write_text(json.dumps(oauth))
        refresh_backup.capture(codex / 'auth.json')
        rotated = json.dumps({**oauth, 'tokens': {'refresh_token': 'rotated-fixture-token'}}).encode()
        (codex / 'auth.json').write_bytes(rotated)
        refresh_backup.restore(preserve_personal_auth=True)
        assert (codex / 'auth.json').read_bytes() == rotated
        refresh_backup.restore()
        assert json.loads((codex / 'auth.json').read_text()) == oauth
        check_windows_permissions(setup, root)
        setup.close_log()
        logs = log_path.read_text(encoding='utf-8')
        for expected in ('bootstrap fixture', 'RUN_START', 'STEP_START', 'STEP_DONE', 'STEP_FAILED', 'elapsed=',
                         'PROCESS_EXIT', 'PROCESS_DENIED', 'code=7', '0x80070005', 'WRITE_BEGIN', 'locked-config.toml',
                         'WinError 32', 'WinError 5', 'REGISTRY_OPEN', 'ROLLBACK_START', 'ROLLBACK_DONE',
                         'CONFIGURATION_COMPLETE runtime_validation=skipped application_launch=skipped', '[KEY]', '[TOKEN]', '[CREDENTIALS]'):
            assert expected in logs, 'missing diagnostic: ' + expected
        for secret in (dummy_key, stale, 'fixture-secret-key', 'fixture-proxy-password', 'fixture-bearer-token',
                       'fixture-refresh-token', 'fixture-apikey-value', 'fixture-process-output', 'sk-process-secret',
                       'sk-never-log-file-contents', 'rotated-fixture-token', 'personal-refresh-token',
                       'sk-public-input-fixture-never-sent'):
            assert secret not in logs, 'sensitive data in logs'
    print('PASS: CC Switch form endpoint/model fields and v2.4.0 upgrade; configuration with ALL application starts denied; TOML values, accounts, keys, logs, proxy cleanup and rollback.')
    print('No installed Codex/CC Switch, real credentials or model requests were used.')


if __name__ == '__main__':
    check()
