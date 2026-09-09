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


def fake(state_path, mode):
    state = json.loads(Path(state_path).read_text())
    directory = Path(state['directory'])
    if mode == 'exec':
        if state.get('hang'):
            time.sleep(60)
        for event in state['events']:
            print(json.dumps(event), flush=True)
        return
    cached_account = state.get('native_account')
    if not cached_account and (directory / 'auth.json').exists():
        auth = json.loads((directory / 'auth.json').read_text())
        if auth.get('auth_mode') == 'chatgpt' or auth.get('tokens'):
            cached_account = {'type': 'chatgpt', 'email': 'fixture@example.invalid', 'planType': 'plus'}
        elif auth.get('OPENAI_API_KEY'):
            cached_account = {'type': 'apiKey'}
    if state.get('hide_account'):
        cached_account = None
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        method, params = request['method'], request.get('params', {})
        result = {}
        if method == 'config/read':
            import tomllib
            config_path = directory / 'config.toml'
            config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
            result = {'config': config, 'layers': [{'name': {'type': 'user', 'file': str(config_path)}}]}
        elif method == 'config/batchWrite':
            lines, tables = ['# existing unrelated setting', 'sandbox_mode = "read-only"'], []
            for edit in params['edits']:
                name, value = edit['keyPath'], edit['value']
                if value is None:
                    continue
                if isinstance(value, dict):
                    tables.append('[' + name + ']\n' + '\n'.join(k + ' = ' + json.dumps(v) for k, v in value.items()))
                else:
                    lines.append(name + ' = ' + json.dumps(value))
            Path(params['filePath']).write_text('\n'.join(lines + tables) + '\n')
            result = {'status': 'ok'}
        elif method == 'account/login/start':
            with (directory / 'login-calls.log').open('a') as file:
                file.write('apiKey login\n')
            if state.get('forbid_login'):
                print(json.dumps({'id': request['id'], 'error': {'code': -1}}), flush=True)
                continue
            (directory / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': params['apiKey']}))
            cached_account = {'type': 'apiKey'}
            result = {'type': 'apiKey'}
        elif method == 'account/read':
            result = {'account': cached_account, 'requiresOpenaiAuth': True}
        print(json.dumps({'id': request['id'], 'result': result}), flush=True)


def check():
    spec = importlib.util.spec_from_file_location('kaizo_setup', Path(__file__).with_name('setup.py'))
    setup = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(setup)
    # All credentials in this check are fictional, never loaded from provider.json.
    dummy_key = 'sk-offline-fixture-key-never-sent'
    stale = 'http://srt:offline@localhost:64321'
    fresh = 'http://srt:other@localhost:54321'
    original_profile = '$env:HTTP_PROXY = "' + stale + '"\nWrite-Output "keep"\n'
    profile = setup.profile_cleanup(original_profile, {stale})
    assert profile.startswith(original_profile)
    assert setup.profile_cleanup(profile, {stale}) == profile
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
        codex.mkdir()
        cc.mkdir()
        db_path = cc / 'cc-switch.db'
        # Required DDL from CC Switch 3.20.2 source; extra provider data must survive.
        with sqlite3.connect(db_path) as db:
            db.executescript('''
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
            ''')
        db.close()
        (codex / 'config.toml').write_text('model = "old"\n')
        (codex / 'AGENTS.md').write_text('Old persona')
        (codex / 'AGENTS.override.md').write_text('Old override')
        (cc / 'settings.json').write_text('{"unrelated": "keep"}')
        originals = {path: path.read_bytes() for path in (codex / 'config.toml', codex / 'AGENTS.md',
                    codex / 'AGENTS.override.md', cc / 'settings.json')}
        backup = setup.Backup(root / 'backup')
        for path in (*originals, codex / 'auth.json'):
            backup.capture(path)
        backup.capture(db_path, database=True)
        state_path = root / 'fake-state.json'
        state = {'directory': str(codex), 'events': [
            {'type': 'turn.started'},
            {'type': 'error', 'message': 'HTTP 503'},
            {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'KAIZO_READY'}},
            {'type': 'turn.completed'}]}
        state_path.write_text(json.dumps(state))
        binary = [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()), '--fake', str(state_path)]
        agents = (Path(__file__).with_name('AGENTS.md')).read_text()
        assert all(word not in agents for word in ('N.O.V.A.', 'Dylan', 'Sir', '女性', 'female'))
        setup.check_db(db_path)
        with setup.Rpc(binary, root) as rpc:
            try:
                setup.verify_account(rpc)
                raise AssertionError('missing API login accepted')
            except setup.Failure:
                pass
        for _ in range(2):
            setup.configure(binary, root, codex, cc, dummy_key, agents)
            with setup.Rpc(binary, root) as rpc:
                config = rpc.call('config/read', {'includeLayers': False})['config']
                setup.verify_config(config, dummy_key)
                setup.verify_account(rpc)
            assert config['sandbox_mode'] == 'read-only'
            assert not (codex / 'AGENTS.override.md').exists()
            assert (codex / 'AGENTS.md').read_text() == agents
            settings = json.loads((cc / 'settings.json').read_text())
            assert settings['preserveCodexOfficialAuthOnSwitch'] is True and settings['unrelated'] == 'keep'
            with sqlite3.connect(db_path) as db:
                saved = db.execute("SELECT settings_config FROM providers WHERE app_type='codex' AND is_current=1").fetchall()
                assert len(saved) == 1 and json.loads(saved[0][0])['auth']['OPENAI_API_KEY'] == dummy_key
                assert json.loads(saved[0][0])['config'] == (codex / 'config.toml').read_text()
                assert db.execute("SELECT is_current FROM providers WHERE app_type='claude'").fetchone() == (1,)
                assert db.execute("SELECT content FROM prompts WHERE app_type='codex' AND enabled=1").fetchall() == [(agents,)]
            db.close()
        assert (codex / 'login-calls.log').read_text().splitlines() == ['apiKey login']
        with sqlite3.connect(db_path) as db:
            personal = json.loads(db.execute("SELECT settings_config FROM providers WHERE id=?", (setup.PERSONAL_PROVIDER,)).fetchone()[0])
        db.close()
        assert personal['auth'] == {} and dummy_key not in json.dumps(personal)
        config['service_tier'] = 'fast'
        try:
            setup.verify_config(config, dummy_key)
            raise AssertionError('Fast override accepted')
        except setup.Failure:
            pass
        messages = []
        setup.smoke_test(binary, root, messages.append, timeout=8)
        assert dummy_key not in '\n'.join(messages)
        assert setup.error_detail({'message': 'HTTP 401 Bearer ' + dummy_key}) == 'HTTP 401'
        for events in ([{'type': 'turn.completed'}],
                       [{'type': 'item.completed', 'item': {'type': 'command_execution', 'text': 'KAIZO_READY'}}, {'type': 'turn.completed'}],
                       state['events'] + [{'type': 'turn.failed', 'error': {'message': 'HTTP 401'}}]):
            state_path.write_text(json.dumps({**state, 'events': events}))
            try:
                setup.smoke_test(binary, root, messages.append, timeout=8)
                raise AssertionError('invalid model completion accepted')
            except setup.Failure:
                pass
        state_path.write_text(json.dumps({**state, 'hang': True}))
        started = time.monotonic()
        try:
            setup.smoke_test(binary, root, messages.append, timeout=0.5)
            raise AssertionError('hanging child accepted')
        except setup.Failure:
            assert time.monotonic() - started < 12
        with sqlite3.connect(db_path) as db:
            db.execute("UPDATE proxy_config SET enabled=1 WHERE app_type='codex'")
        db.close()
        try:
            setup.check_db(db_path)
            raise AssertionError('local takeover accepted')
        except setup.Failure:
            pass
        backup.restore()
        assert all(path.read_bytes() == value for path, value in originals.items())
        assert not (codex / 'auth.json').exists()
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT id FROM providers WHERE app_type='codex'").fetchall() == [('old',)]
        db.close()
        setup.check_db(db_path)

        # Existing ChatGPT logins remain native, including Windows credential-store modes.
        oauth = {'auth_mode': 'chatgpt', 'tokens': {'id_token': 'fixture-id',
                 'access_token': 'personal-access-token', 'refresh_token': 'personal-refresh-token', 'account_id': 'fixture-account'}}
        for store in ('file', 'keyring', 'auto'):
            backup.restore()
            (codex / 'login-calls.log').unlink(missing_ok=True)
            personal_config = '# personal settings\nmodel_provider = "openai"\nmodel = "my-chosen-model"\ncli_auth_credentials_store = "' + store + '"\n'
            (codex / 'config.toml').write_text(personal_config)
            auth_before = json.dumps(oauth).encode() if store == 'file' else None
            if auth_before:
                (codex / 'auth.json').write_bytes(auth_before)
            state_personal = {**state, 'forbid_login': True}
            if store != 'file':
                state_personal['native_account'] = {'type': 'chatgpt', 'email': 'fixture@example.invalid', 'planType': 'plus'}
            state_path.write_text(json.dumps(state_personal))
            for _ in range(2):
                actual_store, expected = setup.configure(binary, root, codex, cc, dummy_key, agents)
                assert actual_store == store and expected['type'] == 'chatgpt'
                assert not (codex / 'login-calls.log').exists()
                assert ((codex / 'auth.json').read_bytes() if (codex / 'auth.json').exists() else None) == auth_before
                with sqlite3.connect(db_path) as db:
                    rows = db.execute("SELECT id,category,settings_config FROM providers WHERE id IN (?,?)", (setup.PERSONAL_PROVIDER, setup.PROVIDER)).fetchall()
                db.close()
                cards = {row[0]: (row[1], json.loads(row[2])) for row in rows}
                assert cards[setup.PERSONAL_PROVIDER] == ('official', {'auth': {}, 'config': personal_config})
                assert cards[setup.PROVIDER][1]['auth'] == {'OPENAI_API_KEY': dummy_key}
                assert 'personal-refresh-token' not in json.dumps(cards[setup.PROVIDER])
            # Project the two stored cards into this fixture using CC Switch's config-only contract.
            # This validates the records and fresh process reads, not the real CC Switch GUI.
            for selected in (setup.PERSONAL_PROVIDER, setup.PROVIDER, setup.PERSONAL_PROVIDER):
                (codex / 'config.toml').write_text(cards[selected][1]['config'])
                with setup.Rpc(binary, root) as rpc:
                    current = rpc.call('config/read', {'includeLayers': False})['config']
                    setup.verify_account(rpc, expected)
                if selected == setup.PERSONAL_PROVIDER:
                    assert current['model_provider'] == 'openai' and current['model'] == 'my-chosen-model'
                    assert dummy_key not in cards[selected][1]['config']
                else:
                    setup.verify_config(current, dummy_key, store)
                assert ((codex / 'auth.json').read_bytes() if (codex / 'auth.json').exists() else None) == auth_before

        # A personal official API key is preserved separately from the KAIZO key.
        backup.restore()
        (codex / 'config.toml').write_text('model_provider = "openai"\n')
        personal_key = 'sk-fictional-personal-official-key'
        (codex / 'auth.json').write_text(json.dumps({'OPENAI_API_KEY': personal_key}))
        state_path.write_text(json.dumps({**state, 'forbid_login': True}))
        setup.configure(binary, root, codex, cc, dummy_key, agents)
        assert json.loads((codex / 'auth.json').read_text())['OPENAI_API_KEY'] == personal_key
        with sqlite3.connect(db_path) as db:
            personal = json.loads(db.execute("SELECT settings_config FROM providers WHERE id=?", (setup.PERSONAL_PROVIDER,)).fetchone()[0])
        db.close()
        assert personal['auth']['OPENAI_API_KEY'] == personal_key and dummy_key not in json.dumps(personal)

        # An unreadable existing login must stop without trying API-key login.
        backup.restore()
        (codex / 'login-calls.log').unlink(missing_ok=True)
        (codex / 'auth.json').write_text(json.dumps(oauth))
        protected = (codex / 'auth.json').read_bytes()
        state_path.write_text(json.dumps({**state, 'forbid_login': True, 'hide_account': True}))
        try:
            setup.configure(binary, root, codex, cc, dummy_key, agents)
            raise AssertionError('unreadable personal credentials replaced')
        except setup.Failure:
            assert (codex / 'auth.json').read_bytes() == protected
            assert not (codex / 'login-calls.log').exists()
        refresh_backup = setup.Backup(root / 'refresh-backup')
        refresh_backup.capture(codex / 'auth.json')
        rotated = json.dumps({**oauth, 'tokens': {**oauth['tokens'], 'refresh_token': 'rotated-fixture-token'}}).encode()
        (codex / 'auth.json').write_bytes(rotated)
        refresh_backup.restore(preserve_personal_auth=True)
        assert (codex / 'auth.json').read_bytes() == rotated
        refresh_backup.restore()
        assert (codex / 'auth.json').read_bytes() == protected
        with sqlite3.connect(db_path) as db:
            db.execute("INSERT INTO providers(id,app_type,name,settings_config,category) VALUES(?,'codex','Existing','{}','third_party')", (setup.PERSONAL_PROVIDER,))
        db.close()
        state_path.write_text(json.dumps({**state, 'forbid_login': True}))
        try:
            setup.configure(binary, root, codex, cc, dummy_key, agents)
            raise AssertionError('colliding provider ID overwritten')
        except setup.Failure as error:
            assert 'ID' in str(error)
        with sqlite3.connect(db_path) as db:
            assert db.execute("SELECT settings_config,category FROM providers WHERE id=?", (setup.PERSONAL_PROVIDER,)).fetchone() == ('{}', 'third_party')
        db.close()
        check_windows_permissions(setup, root)
        setup.close_log()
        logs = log_path.read_text(encoding='utf-8')
        for expected in ('bootstrap fixture', 'RUN_START', 'STEP_START', 'STEP_DONE', 'STEP_FAILED', 'elapsed=',
                         'RPC_START', 'RPC_DONE', 'PROCESS_EXIT', 'PROCESS_DENIED', 'code=7', '0x80070005',
                         'WRITE_BEGIN', 'locked-config.toml', 'WinError 32', 'WinError 5', 'REGISTRY_OPEN',
                         'ROLLBACK_START', 'ROLLBACK_DONE', '[KEY]', '[TOKEN]', '[CREDENTIALS]'):
            assert expected in logs, 'missing diagnostic: ' + expected
        for secret in (dummy_key, stale, 'fixture-secret-key', 'fixture-proxy-password', 'fixture-bearer-token',
                       'fixture-refresh-token', 'fixture-apikey-value', 'fixture-process-output', 'sk-process-secret',
                       'sk-never-log-file-contents', 'rotated-fixture-token', 'fixture@example.invalid'):
            assert secret not in logs, 'sensitive data in logs: ' + secret
        assert 'sk-public-input-fixture-never-sent' not in logs
    print('PASS: hidden key input/cache, verified dependency download, persistent redacted logs, permission fallback, account/provider switching, proxy cleanup, timeouts and rollback.')
    print('No installed Codex/CC Switch, real credentials or network requests were used.')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--fake':
        fake(sys.argv[2], sys.argv[3])
    else:
        check()
