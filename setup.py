"""Windows recipient setup. Standard library only; never run main on the author's Mac."""
import argparse
import base64
import contextlib
import ctypes
from ctypes import wintypes
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import traceback
import urllib.error
import urllib.parse
import urllib.request
import warnings

MODEL = 'gpt-6-astra'
BASE = 'https://kaizo.top/v1'
PROVIDER = 'kaizo-codex-astra-medium'
PERSONAL_PROVIDER = 'kaizo-personal-account'
PROXIES = ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy')
STEPS = ('检查 Windows 与代理', '准备 ChatGPT / Codex', '准备 CC Switch', '备份原配置',
         '配置 KAIZO 与工作规范', '验证登录与模型回复', '打开应用')
ROOT = Path(__file__).resolve().parent
VERSION = '2.3.0'
LOG = logging.getLogger('kaizo.setup')
LOG.setLevel(logging.INFO)
LOG.propagate = False
LOG.addHandler(logging.NullHandler())
LOG_PATH = None


class Failure(Exception):
    pass


def safe_error_text(value):
    text = re.sub(r'(?i)\bsk-[A-Za-z0-9_-]+', '[KEY]', str(value))
    text = re.sub(r'(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+@', r'\1[CREDENTIALS]@', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [TOKEN]', text)
    text = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[TOKEN]', text)
    text = re.sub(r'''(?i)((?:access|refresh|id)_token|api[_-]?key)(["']?\s*[:=]\s*["']?)[^\s"',;}]+''', r'\1\2[TOKEN]', text)
    return re.sub(r'[\x00-\x1f\x7f]', ' ', text)


def redact_log_record(record):
    record.msg, record.args = safe_error_text(record.getMessage()), ()
    # No traceback source lines, locals, request bodies, or subprocess output dumps.
    record.exc_info = record.exc_text = record.stack_info = None
    return True


def close_log():
    global LOG_PATH
    for handler in list(LOG.handlers):
        handler.close()
        LOG.removeHandler(handler)
    LOG.addHandler(logging.NullHandler())
    LOG_PATH = None


def init_log(mode):
    global LOG_PATH
    close_log()
    requested = os.environ.get('KAIZO_SETUP_LOG')
    name = 'setup-' + time.strftime('%Y%m%d-%H%M%S') + '-' + str(os.getpid()) + '.log'
    primary = Path(requested) if requested else Path(os.environ.get('LOCALAPPDATA', tempfile.gettempdir())) / 'KAIZO-Setup/Logs' / name
    for candidate in (primary, ROOT / 'logs' / name):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(candidate, mode='a', encoding='utf-8', errors='backslashreplace')
            handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
            handler.addFilter(redact_log_record)
            LOG.addHandler(handler)
            LOG_PATH = candidate
            LOG.info('RUN_START version=%s mode=%s python=%s os=%s', VERSION, mode, sys.version.split()[0], os.name)
            print('运行日志：' + str(candidate), flush=True)
            return
        except OSError:
            pass
    raise Failure('无法创建运行日志；请将配置包解压到当前用户可写的文件夹后重试。')


def describe_error(error):
    if isinstance(error, Failure):
        return safe_error_text(error)
    parts = [type(error).__name__]
    if getattr(error, 'winerror', None) is not None:
        parts.append('WinError ' + str(error.winerror))
    elif getattr(error, 'errno', None) is not None:
        parts.append('errno ' + str(error.errno))
    if getattr(error, 'strerror', None):
        parts.append(safe_error_text(error.strerror))
    for field in ('filename', 'filename2', 'kaizo_target'):
        value = getattr(error, field, None)
        if value:
            parts.append(safe_error_text(value))
    return ' · '.join(parts)


def report_failure(error, stage='启动检查', directory=None):
    frames = [frame for frame in traceback.extract_tb(error.__traceback__)
              if Path(frame.filename).name == 'setup.py']
    location = ' → '.join(f'{frame.name}:{frame.lineno}' for frame in frames[-3:])
    lines = ['✕ 失败步骤：' + stage, '具体错误：' + describe_error(error)]
    if location:
        lines.append('代码位置：setup.py / ' + location)
    for line in lines:
        LOG.error(line)
    if LOG_PATH:
        lines.append('完整日志：' + str(LOG_PATH))
    lines.append('配置未标记为成功。报告不含密钥、请求正文或环境变量内容。')
    text = '\n'.join(lines)
    print('\n' + text, flush=True)
    report = Path(directory or ROOT) / 'Last-error.txt'
    try:
        report.write_text(text + '\n', encoding='utf-8')
        print('错误报告：' + str(report), flush=True)
    except OSError:
        print('错误报告未能写入文件；请保留上面的具体错误和代码位置。')
    error.kaizo_reported = True


def start_process(args, **options):
    LOG.info('PROCESS_START executable=%s', args[0])
    try:
        proc = subprocess.Popen(args, **options)
        LOG.info('PROCESS_STARTED pid=%s executable=%s', proc.pid, args[0])
        return proc
    except OSError as error:
        LOG.error('PROCESS_DENIED executable=%s error=%s', args[0], describe_error(error))
        raise Failure('无法启动 ' + safe_error_text(args[0]) + '；' + describe_error(error)) from error


@contextlib.contextmanager
def registry_key(winreg, hive, subkey, access, create=False):
    prefix = 'HKCU' if hive == winreg.HKEY_CURRENT_USER else 'HKLM'
    target = prefix + '\\' + subkey
    LOG.info('REGISTRY_OPEN key=%s access=%s create=%s', target, access, create)
    try:
        open_key = winreg.CreateKeyEx if create else winreg.OpenKey
        with open_key(hive, subkey, 0, access) as reg:
            yield reg
    except OSError as error:
        error.kaizo_target = target
        LOG.warning('REGISTRY_ERROR %s', describe_error(error))
        raise


def stop_child(proc):
    if proc.poll() is None:
        LOG.warning('PROCESS_STOP owned_pid=%s', proc.pid)
        if os.name == 'nt':
            subprocess.run([str(Path(os.environ['SystemRoot']) / 'System32/taskkill.exe'),
                            '/PID', str(proc.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            proc.kill()
        proc.wait(timeout=10)


def run(args, *, text='', timeout=60, cwd=None, accepted=(0,)):
    started = time.monotonic()
    proc = start_process([str(x) for x in args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', cwd=cwd,
                            creationflags=0x08000000 if os.name == 'nt' else 0)
    try:
        out, _err = proc.communicate(text, timeout=timeout)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        LOG.warning('PROCESS_TIMEOUT_OR_CANCEL pid=%s elapsed=%.1fs', proc.pid, time.monotonic()-started)
        stop_child(proc)
        raise Failure(f'{Path(args[0]).name} 未在规定时间内完成。') from None
    LOG.info('PROCESS_EXIT pid=%s code=%s elapsed=%.1fs', proc.pid, proc.returncode, time.monotonic()-started)
    if proc.returncode not in accepted:
        codes = sorted(set(re.findall(r'(?i)\b0x[0-9a-f]{8}\b', _err)))
        suffix = ' 系统码：' + ', '.join(codes) if codes else ''
        raise Failure(f'{Path(args[0]).name} 执行失败，退出码 {proc.returncode}。' + suffix)
    return out.strip()


def ps(script, data=None, timeout=60):
    caller = sys._getframe(1)
    LOG.info('POWERSHELL_CALL caller=%s:%s script=%s', caller.f_code.co_name, caller.f_lineno, hashlib.sha256(script.encode()).hexdigest()[:12])
    # Only static code goes on the command line; paths and other data use stdin JSON.
    prelude = "$ErrorActionPreference='Stop'; [Console]::InputEncoding=New-Object Text.UTF8Encoding($false); [Console]::OutputEncoding=New-Object Text.UTF8Encoding($false); "
    if data is not None:
        prelude += '$kaizoData=[Console]::In.ReadToEnd() | ConvertFrom-Json; '
    encoded = base64.b64encode((prelude + script).encode('utf-16le')).decode('ascii')
    exe = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    return run([exe, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
               text=json.dumps(data, ensure_ascii=False) if data is not None else '', timeout=timeout)


def private(path):
    LOG.info('PROTECT_PATH path=%s', path)
    if os.name != 'nt':
        path.chmod(0o700 if path.is_dir() else 0o600)
        return
    if not hasattr(private, 'sid'):
        private.sid = ps('[Security.Principal.WindowsIdentity]::GetCurrent().User.Value')
    sid = private.sid
    rights = '(OI)(CI)(F)' if path.is_dir() else '(F)'
    run([Path(os.environ['SystemRoot']) / 'System32/icacls.exe', path, '/inheritance:r', '/grant:r',
         '*' + sid + ':' + rights, '*S-1-5-18:' + rights, '*S-1-5-32-544:' + rights])


def plain_path(path):
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction()):
            raise Failure(f'拒绝改写链接或目录联接：{candidate.name}')


def atomic(path, content):
    LOG.info('WRITE_BEGIN path=%s', path)
    plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.kaizo-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(content if isinstance(content, bytes) else content.encode('utf-8'))
            file.flush()
            os.fsync(file.fileno())
        private(Path(temporary))
        os.replace(temporary, path)
        LOG.info('WRITE_DONE path=%s', path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_provider():
    local = Path(os.environ['LOCALAPPDATA']) / 'KAIZO-Setup' / 'provider.json'
    legacy = ROOT / 'provider.json'
    source = local if local.exists() else legacy if legacy.exists() else None
    provider = json.loads(source.read_text(encoding='utf-8-sig')) if source else {'url': 'https://kaizo.top'}
    if not isinstance(provider, dict) or not isinstance(provider.get('url'), str) or provider['url'].rstrip('/') != 'https://kaizo.top':
        raise Failure('本包仅支持 https://kaizo.top；请检查本机 provider.json 中的地址。')
    key = os.environ.get('KAIZO_API_KEY', provider.get('key', ''))
    if not isinstance(key, str):
        raise Failure('本机 API Key 格式无效。')
    key = key.strip()
    if not key:
        print('公开版不附带密钥。请输入你自己的 KAIZO Key；以后更新会复用本机保存的 Key。')
        with warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            try:
                key = getpass.getpass('KAIZO API Key（输入隐藏）：').strip()
            except (getpass.GetPassWarning, EOFError):
                raise Failure('当前窗口无法隐藏输入，请在 Windows 终端双击 Setup.cmd。') from None
    if not re.fullmatch(r'sk-[A-Za-z0-9_-]{16,}', key):
        raise Failure('API Key 格式无效；未保存。')
    desired = {'url': 'https://kaizo.top', 'key': key}
    if source != local or provider != desired:
        plain_path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        private(local.parent)
        atomic(local, json.dumps(desired, ensure_ascii=False, indent=2) + '\n')
    LOG.info('PROVIDER_READY source=%s local_cache=%s', 'environment' if 'KAIZO_API_KEY' in os.environ else 'local-or-input', local)
    return key


class Dashboard:
    def __init__(self):
        self.states = ['pending'] * len(STEPS)
        self.details = [''] * len(STEPS)
        self.started = [0.0] * len(STEPS)
        self.lock = threading.RLock()
        self.quit = threading.Event()
        self.animated = sys.stdout.isatty() and 'NO_COLOR' not in os.environ
        if self.animated and os.name == 'nt':
            kernel = ctypes.windll.kernel32
            kernel.GetStdHandle.argtypes, kernel.GetStdHandle.restype = [wintypes.DWORD], wintypes.HANDLE
            kernel.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
            kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            handle = kernel.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            self.animated = bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)) and
                                 ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 4))

    def start(self):
        if self.animated:
            print('\x1b[?1049h\x1b[?25l', end='', flush=True)
            self.thread = threading.Thread(target=self.animate, daemon=True)
            self.thread.start()

    def animate(self):
        while not self.quit.wait(0.15):
            self.render()

    def render(self):
        with self.lock:
            lines = ['', '  KAIZO  /  WINDOWS SETUP', '  ' + '━' * 52,
                     '  GPT-6 Astra  ·  Medium reasoning  ·  Fast OFF', '']
            for i, title in enumerate(STEPS):
                icon, color = {'pending': ('○', '2'), 'running': ('◐◓◑◒'[int(time.monotonic()*5)%4], '36'),
                               'done': ('✓', '32'), 'failed': ('✕', '31')}[self.states[i]]
                elapsed = f' · {int(time.monotonic()-self.started[i])} 秒' if self.states[i] == 'running' else ''
                text = f'  {icon}  {i+1:02}  {title}{elapsed}'
                lines += [f'\x1b[{color}m{text}\x1b[0m' if self.animated else text,
                          '         ' + self.details[i]]
            done = self.states.count('done')
            lines += ['', '  ' + '━' * (done*28//7) + '─' * (28-done*28//7) + f'  {done*100//7}%',
                      '  密钥隐藏  ·  自动备份  ·  失败恢复']
            if LOG_PATH:
                lines.append('  日志：' + LOG_PATH.name)
            print(('\x1b[H\x1b[2J' if self.animated else '') + '\n'.join(lines), flush=True)

    def detail(self, i, value):
        with self.lock:
            if self.details[i] == value:
                return
            self.details[i] = value
        LOG.info('STEP_%02d %s', i+1, value)
        if not self.animated:
            print('  ' + value, flush=True)

    @contextlib.contextmanager
    def step(self, i, value):
        self.states[i], self.started[i] = 'running', time.monotonic()
        LOG.info('STEP_START %02d %s', i+1, STEPS[i])
        self.detail(i, value)
        try:
            yield
            self.states[i] = 'done'
            LOG.info('STEP_DONE %02d elapsed=%.1fs', i+1, time.monotonic()-self.started[i])
        except BaseException as error:
            self.states[i] = 'failed'
            LOG.error('STEP_FAILED %02d elapsed=%.1fs error=%s', i+1, time.monotonic()-self.started[i], describe_error(error))
            raise

    def stop(self):
        if not self.quit.is_set():
            self.quit.set()
            if self.animated:
                self.thread.join(2)
                print('\x1b[?25h\x1b[?1049l', end='', flush=True)


class Backup:
    def __init__(self, directory):
        self.directory = directory
        LOG.info('BACKUP_CREATE directory=%s', directory)
        directory.mkdir(parents=True, exist_ok=False)
        private(directory)
        self.entries = []
        self.registry = []

    def save_manifest(self):
        atomic(self.directory / 'manifest.json', json.dumps({'files': self.entries, 'registry': self.registry}, ensure_ascii=False, indent=2))

    def capture(self, path, database=False):
        plain_path(path)
        if any(entry['path'] == str(path) for entry in self.entries):
            return
        LOG.info('BACKUP_FILE path=%s database=%s', path, database)
        slot = str(len(self.entries))
        if path.exists():
            if database:
                with sqlite3.connect(path) as source, sqlite3.connect(self.directory / slot) as target:
                    source.backup(target)
            else:
                shutil.copyfile(path, self.directory / slot)
        self.entries.append({'path': str(path), 'slot': slot, 'existed': path.exists(), 'database': database})
        self.save_manifest()

    def restore(self, preserve_personal_auth=False):
        LOG.info('ROLLBACK_START directory=%s preserve_personal_auth=%s', self.directory, preserve_personal_auth)
        for entry in reversed(self.entries):
            path = Path(entry['path'])
            plain_path(path)
            if preserve_personal_auth and entry['existed'] and path.name == 'auth.json':
                original = json.loads((self.directory / entry['slot']).read_text(encoding='utf-8-sig'))
                if has_auth_material(original):
                    # Setup never overwrites existing credentials. Keep any native token refresh on failure.
                    LOG.info('ROLLBACK_KEEP_PERSONAL_AUTH path=%s', path)
                    continue
            LOG.info('ROLLBACK_FILE path=%s', path)
            if not entry['existed']:
                path.unlink(missing_ok=True)
            elif entry['database']:
                with sqlite3.connect(self.directory / entry['slot']) as source, sqlite3.connect(path) as target:
                    source.backup(target)
            else:
                atomic(path, (self.directory / entry['slot']).read_bytes())
        if self.registry:
            import winreg
            with registry_key(winreg, winreg.HKEY_CURRENT_USER, 'Environment', winreg.KEY_SET_VALUE, create=True) as reg:
                for item in self.registry:
                    winreg.SetValueEx(reg, item['name'], 0, item['type'], item['value'])
            broadcast_environment()
        LOG.info('ROLLBACK_DONE directory=%s', self.directory)


class Rpc:
    def __init__(self, binary, cwd):
        command = list(binary) if isinstance(binary, (list, tuple)) else [str(binary)]
        self.proc = start_process(command + ['app-server'], cwd=cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace',
            creationflags=0x08000000 if os.name == 'nt' else 0)
        self.events = queue.Queue()
        self.ident = 0
        threading.Thread(target=self.read, daemon=True).start()
        threading.Thread(target=lambda: self.proc.stderr.read(), daemon=True).start()
        try:
            self.call('initialize', {'clientInfo': {'name': 'kaizo_setup', 'title': 'KAIZO Setup', 'version': '2.0'}})
            self.write({'method': 'initialized', 'params': {}})
        except BaseException:
            self.close()
            raise

    def read(self):
        for line in self.proc.stdout:
            try:
                self.events.put(json.loads(line))
            except json.JSONDecodeError:
                pass
        self.events.put(None)

    def write(self, message):
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False) + '\n')
        self.proc.stdin.flush()

    def call(self, method, params):
        self.ident += 1
        started = time.monotonic()
        LOG.info('RPC_START method=%s id=%s', method, self.ident)
        self.write({'id': self.ident, 'method': method, 'params': params})
        deadline = time.monotonic() + 45
        while True:
            try:
                message = self.events.get(timeout=max(0, deadline-time.monotonic()))
            except queue.Empty:
                raise Failure(f'Codex 的 {method} 响应超时。') from None
            if message is None:
                raise Failure('Codex 配置服务提前退出；请检查桌面应用是否完整安装。')
            if message.get('id') == self.ident:
                if 'error' in message:
                    raise Failure(f'Codex 拒绝 {method}，错误码 {message["error"].get("code")}。')
                LOG.info('RPC_DONE method=%s elapsed=%.1fs', method, time.monotonic()-started)
                return message['result']

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            stop_child(self.proc)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def edits(key, auth_store='file'):
    values = {'model_provider': 'custom', 'model': MODEL, 'model_reasoning_effort': 'medium',
        'service_tier': None, 'features.fast_mode': False, 'personality': 'none', 'profile': None,
        'cli_auth_credentials_store': auth_store, 'disable_response_storage': True,
        'model_catalog_json': None, 'model_context_window': None, 'model_auto_compact_token_limit': None,
        'model_providers.custom': {'name': 'KAIZO', 'base_url': BASE, 'wire_api': 'responses',
            'requires_openai_auth': True, 'experimental_bearer_token': key, 'supports_websockets': False}}
    return [{'keyPath': name, 'value': value, 'mergeStrategy': 'replace'} for name, value in values.items()]


def verify_config(config, key, auth_store='file'):
    provider = config.get('model_providers', {}).get('custom', {})
    valid = config.get('model_provider') == 'custom' and config.get('model') == MODEL
    valid &= config.get('model_reasoning_effort') == 'medium' and config.get('service_tier') in (None, 'default')
    valid &= config.get('features', {}).get('fast_mode') is False and config.get('cli_auth_credentials_store') == auth_store
    valid &= all(provider.get(k) == v for k, v in {'base_url': BASE, 'wire_api': 'responses',
        'requires_openai_auth': True, 'experimental_bearer_token': key, 'supports_websockets': False}.items())
    if not valid:
        raise Failure('生效配置与 Astra / Medium / Fast OFF 不一致，可能存在配置档或策略覆盖。')


def has_auth_material(auth):
    return any(auth.get(name) for name in ('OPENAI_API_KEY', 'tokens', 'personal_access_token',
                                          'agent_identity', 'bedrock_api_key', 'bedrock_access_keys'))


def verify_account(rpc, expected=None, allow_missing=False):
    state = rpc.call('account/read', {'refreshToken': False})
    account = state.get('account')
    if not account and allow_missing and state.get('requiresOpenaiAuth'):
        return None
    if not isinstance(account, dict) or account.get('type') not in ('apiKey', 'chatgpt') or not state.get('requiresOpenaiAuth'):
        raise Failure('Codex 未识别持久登录状态，不会标记为成功。')
    if expected and any(expected.get(field) != account.get(field) for field in ('type', 'email')):
        raise Failure('原有账户状态发生变化，已停止，避免覆盖个人登录。')
    return account


def check_db(path):
    if not path.is_file():
        raise Failure('CC Switch 尚未初始化数据库。')
    with sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True) as db:
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise Failure('CC Switch 数据库完整性检查失败。')
        # ponytail: schema 18 is the audited CC Switch 3.20.2 compatibility boundary.
        if db.execute('PRAGMA user_version').fetchone()[0] != 18:
            raise Failure('仅支持 CC Switch schema 18，未改写不兼容数据库。')
        for table, expected in {'providers': {'id','app_type','settings_config','meta','is_current'},
                                'prompts': {'id','app_type','content','enabled'},
                                'proxy_config': {'app_type','enabled','auto_failover_enabled'}}.items():
            columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
            if not expected <= columns:
                raise Failure('CC Switch 数据库结构不兼容。')
        row = db.execute("SELECT enabled OR auto_failover_enabled FROM proxy_config WHERE app_type='codex'").fetchone()
        if row and row[0]:
            raise Failure('请在 CC Switch 关闭 Codex 本地路由和自动故障转移，再运行。')


def configure(binary, work, codex, cc, key, agents):
    path = codex / 'config.toml'
    original_config = path.read_text(encoding='utf-8-sig') if path.exists() else ''
    auth_path = codex / 'auth.json'
    original_auth = auth_path.read_bytes() if auth_path.exists() else None
    auth_value = json.loads(original_auth.decode('utf-8-sig')) if original_auth else {}
    with Rpc(binary, work) as rpc:
        before = rpc.call('config/read', {'includeLayers': True})
        for layer in before.get('layers') or []:
            source = layer.get('name') or layer.get('source') or {}
            if source.get('type') == 'user' and (source.get('profile') or
                (source.get('file') and os.path.normcase(os.path.abspath(source['file'])) != os.path.normcase(str(path)))):
                raise Failure('Codex 配置目录或配置档与目标不一致。')
        previous = before['config']
        auth_store = previous.get('cli_auth_credentials_store') or 'file'
        if auth_store not in ('file', 'keyring', 'auto'):
            raise Failure('原登录使用临时凭据存储，无法安全保留；请先在桌面应用完成持久登录。')
        official_route = (previous.get('model_provider') or 'openai') == 'openai' and not previous.get('openai_base_url')
        result = rpc.call('config/batchWrite', {'edits': edits(key, auth_store), 'filePath': str(path)})
        if result.get('status') != 'ok':
            raise Failure('配置写入被更高优先级设置覆盖。')
    private(path)
    with Rpc(binary, work) as rpc:
        effective = rpc.call('config/read', {'includeLayers': False})['config']
        verify_config(effective, key, auth_store)
        account = verify_account(rpc, allow_missing=True)
        if effective.get('forced_login_method') == 'chatgpt' and (account or {}).get('type') != 'chatgpt':
            raise Failure('当前策略要求 ChatGPT 账户；请先完成个人账户登录。')
        if account is None:
            # Never overwrite an unreadable/expired personal login or a Windows credential-store entry.
            if auth_store != 'file' or has_auth_material(auth_value):
                raise Failure('已有登录凭据未被 Codex 正确识别，已保留。请先在原应用重新登录，再运行本包。')
            if rpc.call('account/login/start', {'type': 'apiKey', 'apiKey': key}).get('type') != 'apiKey':
                raise Failure('Codex 没有完成原生 API Key 登录。')
            account = verify_account(rpc)
            if json.loads(auth_path.read_text(encoding='utf-8')).get('OPENAI_API_KEY') != key:
                raise Failure('API Key 登录未保存到预期目录。')
    if auth_path.exists():
        private(auth_path)
    config = path.read_text(encoding='utf-8')
    verify_config(tomllib.loads(config), key, auth_store)
    settings_config = json.dumps({'auth': {'OPENAI_API_KEY': key}, 'config': config}, ensure_ascii=False)
    # An unbound official card follows the live ChatGPT login. CC Switch backfills it on switch-away.
    # Preserve an existing official API key explicitly; never place the KAIZO key on the official card.
    personal_auth = auth_value if official_route and account['type'] == 'apiKey' and auth_value.get('OPENAI_API_KEY') not in (None, '', key) else {}
    personal_config = original_config if official_route else f'model_provider = "openai"\ncli_auth_credentials_store = "{auth_store}"\n'
    personal_settings = json.dumps({'auth': personal_auth, 'config': personal_config}, ensure_ascii=False)
    with sqlite3.connect(cc / 'cc-switch.db') as db:
        db.execute('PRAGMA foreign_keys=ON')
        existing = db.execute("SELECT category FROM providers WHERE id=? AND app_type='codex'", (PERSONAL_PROVIDER,)).fetchone()
        if existing and existing[0] != 'official':
            raise Failure('个人账户条目与既有供应商 ID 冲突，未覆盖该条目。')
        if not existing:
            db.execute("""INSERT INTO providers(id,app_type,name,settings_config,website_url,category,created_at,meta,is_current)
                VALUES(?,'codex','我的 ChatGPT 账户',?,'https://chatgpt.com/codex','official',?,?,0)""",
                (PERSONAL_PROVIDER, personal_settings, int(time.time()*1000), json.dumps({'commonConfigEnabled': False})))
        elif official_route:
            # Capture settings changed while using the personal account; repairs on KAIZO leave its card alone.
            db.execute("UPDATE providers SET settings_config=? WHERE id=? AND app_type='codex'",
                       (personal_settings, PERSONAL_PROVIDER))
        db.execute("UPDATE providers SET is_current=0 WHERE app_type='codex'")
        db.execute("""INSERT INTO providers(id,app_type,name,settings_config,website_url,category,created_at,meta,is_current)
            VALUES(?,'codex',?,?,'https://kaizo.top','third_party',?,?,1)
            ON CONFLICT(id,app_type) DO UPDATE SET name=excluded.name,settings_config=excluded.settings_config,
              meta=excluded.meta,is_current=1""", (PROVIDER, 'KAIZO · Astra / Medium', settings_config, int(time.time()*1000),
                json.dumps({'commonConfigEnabled': False, 'apiFormat': 'openai_responses'})))
        db.execute("UPDATE prompts SET enabled=0 WHERE app_type='codex'")
        db.execute("""INSERT INTO prompts(id,app_type,name,content,description,enabled,created_at,updated_at)
            VALUES('kaizo-general-agents','codex','General Working Guidelines',?,'通用工作规范',1,?,?)
            ON CONFLICT(id,app_type) DO UPDATE SET content=excluded.content,description=excluded.description,
              enabled=1,updated_at=excluded.updated_at""", (agents, int(time.time()*1000), int(time.time()*1000)))
    settings_path = cc / 'settings.json'
    settings = json.loads(settings_path.read_text(encoding='utf-8-sig')) if settings_path.exists() else {}
    settings.update({'currentProviderCodex': PROVIDER, 'preserveCodexOfficialAuthOnSwitch': True})
    atomic(settings_path, json.dumps(settings, ensure_ascii=False, indent=2))
    private(cc / 'cc-switch.db')
    atomic(codex / 'AGENTS.md', agents)
    (codex / 'AGENTS.override.md').unlink(missing_ok=True)
    check_db(cc / 'cc-switch.db')
    with sqlite3.connect(cc / 'cc-switch.db') as db:
        saved = db.execute("SELECT settings_config FROM providers WHERE id=? AND app_type='codex' AND is_current=1", (PROVIDER,)).fetchone()
    if not saved or saved[0] != settings_config:
        raise Failure('CC Switch 供应商回读不一致。')
    return auth_store, account


def read_encoded(path):
    data = path.read_bytes()
    encoding = 'utf-16' if data.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig' if data.startswith(b'\xef\xbb\xbf') else 'utf-8'
    try:
        return data.decode(encoding), encoding
    except UnicodeDecodeError:
        raise Failure(f'{path.name} 的编码无法安全处理，未改写。') from None


def proxy_url(value):
    try:
        uri = urllib.parse.urlsplit(value if '://' in value else 'http://' + value)
        if uri.hostname not in ('localhost', '127.0.0.1', '::1'):
            return None
        port = uri.port or (1080 if uri.scheme.startswith('socks') else 80)
        return uri.hostname, port
    except ValueError:
        return None


def is_dead(value):
    address = proxy_url(value)
    if not address:
        return False
    try:
        with socket.create_connection(address, timeout=1):
            return False
    except ConnectionRefusedError:
        return True
    except OSError:
        # A timeout/unknown network error is insufficient evidence to remove a proxy.
        return False


def direct_probe():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(BASE + '/models', timeout=10) as response:
            return response.status == 200
    except urllib.error.HTTPError as error:
        return error.code == 401
    except (OSError, urllib.error.URLError):
        return False


def broadcast_environment():
    if os.name == 'nt':
        result = ctypes.c_size_t()
        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR,
                         wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        send.restype = ctypes.c_ssize_t
        send(0xffff, 0x001A, 0, 'Environment', 2, 2000, ctypes.byref(result))


def profile_cleanup(text, stale):
    fingerprint = hashlib.sha256('\n'.join(sorted(stale)).encode()).hexdigest()[:12]
    marker = '# KAIZO expired proxy cleanup ' + fingerprint
    if marker in text:
        return text
    # Compare complete expired values. A future proxy with a different token remains usable.
    values = ','.join("'" + value.replace("'", "''") + "'" for value in sorted(stale))
    names = ','.join("'" + name + "'" for name in PROXIES)
    return text + f"\n{marker}\nforeach ($kaizoProxyName in @({names})) {{\n" + \
        f"    if (@({values}) -ccontains [Environment]::GetEnvironmentVariable($kaizoProxyName,'Process')) {{\n" + \
        "        [Environment]::SetEnvironmentVariable($kaizoProxyName,$null,'Process')\n    }\n}\n" + \
        "Remove-Variable kaizoProxyName -ErrorAction SilentlyContinue\n"


def dotenv_cleanup(text, stale):
    lines = []
    for line in text.splitlines(keepends=True):
        match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$', line)
        if match and match[1] in PROXIES:
            value = match[2].strip()
            if value[:1] in ('"', "'"):
                quote = value[0]
                close = value.find(quote, 1)
                value = value[1:close] if close > 0 and (not value[close+1:].strip() or value[close+1:].lstrip().startswith('#')) else None
            else:
                value = re.split(r'\s+#', value, maxsplit=1)[0]
            if value in stale:
                lines.append('# KAIZO: removed an expired proxy assignment\n')
                continue
        lines.append(line)
    return ''.join(lines)


def proxy_preflight(home, codex, backup, detail):
    import winreg
    sources = [('Process', name, value, None) for name, value in os.environ.items() if name.upper() in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY')]
    for label, hive, subkey in [('User', winreg.HKEY_CURRENT_USER, 'Environment'),
        ('Machine', winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\Session Manager\Environment')]:
        try:
            with registry_key(winreg, hive, subkey, winreg.KEY_QUERY_VALUE) as reg:
                for i in range(winreg.QueryInfoKey(reg)[1]):
                    name, value, kind = winreg.EnumValue(reg, i)
                    if name.upper() in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY') and isinstance(value, str):
                        sources.append((label, name, value, kind))
        except FileNotFoundError:
            pass
    documents = Path(ps("[Environment]::GetFolderPath('MyDocuments')"))
    profiles = [documents / directory / name for directory in ('WindowsPowerShell', 'PowerShell')
                for name in ('profile.ps1', 'Microsoft.PowerShell_profile.ps1')]
    files = [codex / '.env'] + profiles
    file_text = {}
    for path in files:
        if path.is_file():
            text, encoding = read_encoded(path)
            file_text[path] = (text, encoding)
            for value in re.findall(r'''https?://[^\s"'<>]+''', text):
                if proxy_url(value):
                    sources.append((str(path), '', value, None))
    cache = {value: is_dead(value) for _, _, value, _ in sources}
    stale = {value for value, dead in cache.items() if dead}
    # Windows system proxy is a distinct route. Do not silently change all applications' policy.
    try:
        with registry_key(winreg, winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Internet Settings', winreg.KEY_QUERY_VALUE) as reg:
            enabled = winreg.QueryValueEx(reg, 'ProxyEnable')[0]
            server = winreg.QueryValueEx(reg, 'ProxyServer')[0] if enabled else ''
            for part in str(server).split(';'):
                value = part.split('=', 1)[-1]
                if value and is_dead(value):
                    raise Failure('Windows 系统代理指向未运行的本地端口。请在 设置 → 网络和 Internet → 代理 中修正后再运行。')
    except FileNotFoundError:
        pass
    if any(label == 'Machine' and value in stale for label, _, value, _ in sources):
        raise Failure('系统级环境变量含失效代理；请由管理员修正对应代理变量后重试。')
    if not stale:
        detail('未发现可确认失效的本地代理；现有正常代理保留')
        return
    detail('发现未运行的本地代理，正在确认 KAIZO HTTPS 直连')
    if not direct_probe():
        raise Failure('本地代理失效，KAIZO HTTPS 直连也未通过；尚未修改代理设置。')
    for label, name, value, kind in sources:
        if value in stale:
            address = proxy_url(value)
            detail(f'失效代理来源：{label} {name} · {address[0]}:{address[1]}')
    # Prepare all edits and backups before persistent mutation.
    plans = []
    for path, (text, encoding) in file_text.items():
        relevant = {value for value in stale if value in text}
        if not relevant:
            continue
        updated = dotenv_cleanup(text, relevant) if path.name == '.env' else profile_cleanup(text, relevant)
        if path.name == '.env' and any(value in updated for value in relevant):
            raise Failure('Codex .env 中的代理声明不是可安全处理的独立赋值，请先检查该文件。')
        if path.suffix == '.ps1':
            ps("$tokens=$null; $parseErrors=$null; [System.Management.Automation.Language.Parser]::ParseInput($kaizoData.text,[ref]$tokens,[ref]$parseErrors) | Out-Null; if($parseErrors.Count -gt 0){exit 2}", {'text': updated})
        backup.capture(path)
        plans.append((path, updated.encode(encoding)))
    for label, name, value, kind in sources:
        if label == 'User' and value in stale:
            backup.registry.append({'name': name, 'value': value, 'type': kind})
    backup.save_manifest()
    for path, content in plans:
        atomic(path, content)
    if backup.registry:
        with registry_key(winreg, winreg.HKEY_CURRENT_USER, 'Environment', winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as reg:
            for item in backup.registry:
                if winreg.QueryValueEx(reg, item['name'])[0] != item['value']:
                    raise Failure('代理环境变量在修复期间变化，已停止。')
                winreg.DeleteValue(reg, item['name'])
    for name in list(os.environ):
        if name.upper() in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY') and os.environ[name] in stale:
            del os.environ[name]
    broadcast_environment()
    detail('已备份并清理确认失效的代理；后续终端启动不会重新注入这些值')


def applications():
    raw = ps(r"""
    $kaizoApps=@()
    foreach($p in @(Get-AppxPackage | Where-Object { $_.Name -in @('OpenAI.Codex','OpenAI.ChatGPT') })) {
        $m=Get-AppxPackageManifest -Package $p.PackageFullName
        foreach($a in @($m.Package.Applications.Application)) {
            $exe=Join-Path $p.InstallLocation ([string]$a.Executable)
            if(-not (Test-Path -LiteralPath $exe)){continue}
            $engines=@(Get-ChildItem -LiteralPath $p.InstallLocation -Filter 'codex.exe' -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.FullName -match '\\resources\\' })
            if($engines.Count -gt 0){$kaizoApps+=@{app=$exe;binary=$engines[0].FullName;aumid=($p.PackageFamilyName+'!'+$a.Id);version=[string]$p.Version}}
        }
    }
    ConvertTo-Json -InputObject @($kaizoApps) -Compress
    """)
    return json.loads(raw or '[]')


def processes():
    raw = ps("$p=@(Get-Process -Name Codex,ChatGPT,cc-switch -ErrorAction SilentlyContinue | Select-Object Id,ProcessName,Path); ConvertTo-Json -InputObject @($p) -Compress")
    return json.loads(raw or '[]')


def wait_closed(detail, timeout=300):
    deadline = time.monotonic() + timeout
    while processes():
        detail('等待退出 ChatGPT / Codex 和 CC Switch；请保存工作，并在托盘菜单选择退出')
        if time.monotonic() > deadline:
            raise Failure('等待应用退出超时；配置没有被标记为成功。')
        time.sleep(2)


def download(url, destination, detail, timeout=600):
    # Native curl has bounded transfers and ignores user curl configuration.
    curl = Path(os.environ['SystemRoot']) / 'System32/curl.exe'
    detail('从官方来源下载，首次安装可能需要几分钟')
    run([curl, '-q', '--fail', '--location', '--silent', '--show-error', '--proto', '=https',
         '--proto-redir', '=https', '--connect-timeout', '15', '--max-time', str(timeout),
         '--output', destination, url], timeout=timeout+10)


def prepare_codex(work, arch, repair, detail):
    installed = applications()
    if installed:
        return installed[0]
    if repair:
        raise Failure('未找到包含 Codex 运行组件的桌面应用；请运行 Setup.cmd。')
    winget = shutil.which('winget.exe')
    if winget:
        try:
            detail('通过官方 Microsoft Store 产品 ID 安装 ChatGPT / Codex')
            run([winget, 'install', '--id', '9PLM9XGG6VKS', '-s', 'msstore', '--accept-package-agreements',
                 '--accept-source-agreements', '--disable-interactivity'], timeout=900)
        except Failure as error:
            detail('商店安装不可用：' + describe_error(error) + '；改用官方签名 MSIX')
        installed = applications()
        if installed:
            return installed[0]
    package = work / ('ChatGPT-' + arch + '.msix')
    download('https://persistent.oaistatic.com/codex-app-prod/' + package.name, package, detail)
    # Add-AppxPackage enforces package signatures and OS/dependency compatibility.
    ps('Add-AppxPackage -Path $kaizoData.path', {'path': str(package)}, timeout=600)
    installed = applications()
    if not installed:
        raise Failure('安装后未发现 Codex 运行组件。请确认系统兼容性及应用安装状态。')
    return installed[0]


def find_cc():
    raw = ps(r"""
    $kaizoCandidates=@((Join-Path $env:LOCALAPPDATA 'Programs\CC Switch'),(Join-Path $env:ProgramFiles 'CC Switch'))
    $kaizoRegistry=@('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*')
    foreach($r in $kaizoRegistry){foreach($p in @(Get-ItemProperty $r -ErrorAction SilentlyContinue | Where-Object {$_.DisplayName -eq 'CC Switch'})){if($p.InstallLocation){$kaizoCandidates+=$p.InstallLocation}}}
    $kaizoResult=@()
    foreach($base in $kaizoCandidates | Select-Object -Unique){$exe=Join-Path $base 'cc-switch.exe'; if(Test-Path -LiteralPath $exe){$f=Get-Item -LiteralPath $exe; $kaizoResult+=@{app=$exe;version=$f.VersionInfo.ProductVersion}}}
    ConvertTo-Json -InputObject @($kaizoResult) -Compress
    """)
    return json.loads(raw or '[]')


def version_tuple(value):
    match = re.match(r'^(\d+)\.(\d+)\.(\d+)', value or '')
    return tuple(map(int, match.groups())) if match else (0, 0, 0)


def prepare_cc(arch, repair, detail):
    installed = find_cc()
    if installed and version_tuple(installed[0]['version']) >= (3, 20, 2):
        return installed[0]['app']
    if repair:
        raise Failure('未找到兼容的 CC Switch；请运行 Setup.cmd。')
    manifest = json.loads((ROOT / 'assets/manifest.json').read_text())
    asset = manifest[arch]['cc']
    installer = ROOT / 'assets' / asset['file']
    if not installer.exists():
        installer = Path(os.environ['LOCALAPPDATA']) / 'KAIZO-Setup' / 'Downloads' / asset['file']
        plain_path(installer)
        installer.parent.mkdir(parents=True, exist_ok=True)
        if not installer.exists() or hashlib.sha256(installer.read_bytes()).hexdigest() != asset['sha256']:
            with tempfile.TemporaryDirectory(prefix='cc-download-', dir=installer.parent) as temporary:
                fetched = Path(temporary) / asset['file']
                detail('首次准备 CC Switch：从官方发布页下载并校验')
                LOG.info('ASSET_DOWNLOAD_START file=%s', asset['file'])
                download(asset['url'], fetched, detail)
                if hashlib.sha256(fetched.read_bytes()).hexdigest() != asset['sha256']:
                    raise Failure('下载的 CC Switch 校验失败，未运行安装器。')
                os.replace(fetched, installer)
                LOG.info('ASSET_DOWNLOAD_DONE file=%s', asset['file'])
    if hashlib.sha256(installer.read_bytes()).hexdigest() != asset['sha256']:
        raise Failure('CC Switch 官方安装包校验失败。')
    detail('安装已校验的 CC Switch 3.20.2；保持系统签名与安装保护')
    run([Path(os.environ['SystemRoot']) / 'System32/msiexec.exe', '/i', installer, '/qn', '/norestart'],
        timeout=300, accepted=(0, 3010))
    installed = find_cc()
    if not installed or version_tuple(installed[0]['version']) < (3, 20, 2):
        raise Failure('CC Switch 安装未完成；请查看系统安装提示。')
    return installed[0]['app']


def initialize_cc(app, cc, backup, detail):
    db = cc / 'cc-switch.db'
    if db.exists():
        with sqlite3.connect(db) as conn:
            schema = conn.execute('PRAGMA user_version').fetchone()[0]
        if schema == 18:
            return
        if schema > 18:
            raise Failure('CC Switch 数据库版本高于脚本兼容范围。')
        backup.capture(db, database=True)
    settings_path = cc / 'settings.json'
    backup.capture(settings_path)
    settings = json.loads(settings_path.read_text(encoding='utf-8-sig')) if settings_path.exists() else {}
    previous = settings.get('minimizeToTrayOnClose')
    settings['minimizeToTrayOnClose'] = False
    atomic(settings_path, json.dumps(settings, ensure_ascii=False, indent=2))

    child = start_process([app])
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if db.exists():
            try:
                with sqlite3.connect(db, timeout=1) as conn:
                    if conn.execute('PRAGMA user_version').fetchone()[0] == 18:
                        break
            except sqlite3.Error:
                pass
        if child.poll() is not None:
            raise Failure('CC Switch 初始化时退出。')
        time.sleep(0.5)
    else:
        raise Failure('CC Switch 初始化超时。')
    ps('$p=Get-Process -Id $kaizoData.id -ErrorAction SilentlyContinue; if($p){$null=$p.CloseMainWindow()}', {'id': child.pid})
    wait_closed(detail)
    settings = json.loads(settings_path.read_text(encoding='utf-8-sig'))
    if previous is None:
        settings.pop('minimizeToTrayOnClose', None)
    else:
        settings['minimizeToTrayOnClose'] = previous
    atomic(settings_path, json.dumps(settings, ensure_ascii=False, indent=2))


def error_detail(event):
    raw = str(event.get('message', '')) + ' ' + str((event.get('error') or {}).get('message', '') if isinstance(event.get('error'), dict) else '')
    status = re.search(r'\b(?:HTTP(?:/\d(?:\.\d)?)?(?:\s+status)?|status(?:\s+code)?)\s*[:=(]?\s*([45]\d{2})\b', raw, re.I)
    if status:
        return 'HTTP ' + status[1]
    for pattern, text in [(r'localhost|127\.0\.0\.1|\[::1\]', '本地代理端口连接失败'),
                          (r'certificate|tls|ssl', 'TLS / 证书验证失败'),
                          (r'timeout|timed?\s*out', '请求或连接超时'),
                          (r'dns|resolve', '域名解析失败'),
                          (r'windows.*sandbox|sandbox.*windows', 'Windows 沙箱尚未就绪'),
                          (r'stream.*disconnect', '响应流中断'),
                          (r'connect|error sending request', '请求发送失败')]:
        if re.search(pattern, raw, re.I):
            return text
    return '模型请求尚未完成，请检查网络或服务状态'


def smoke_test(binary, work, detail, timeout=180):
    command = list(binary) if isinstance(binary, (list, tuple)) else [str(binary)]
    args = command + ['exec', '--ephemeral', '--skip-git-repo-check', '--sandbox', 'read-only',
            '-C', str(work), '--json', 'Connectivity test only. Do not use tools, read files, or execute commands. Reply exactly KAIZO_READY.']
    proc = start_process(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding='utf-8', errors='replace', creationflags=0x08000000 if os.name == 'nt' else 0)
    events = queue.Queue()

    def read():
        for line in proc.stdout:
            try:
                events.put(json.loads(line))
            except json.JSONDecodeError:
                pass
        events.put(None)

    threading.Thread(target=read, daemon=True).start()
    threading.Thread(target=lambda: proc.stderr.read(), daemon=True).start()
    completed = failed = False
    replies = []
    latest = '等待 Codex 开始请求'
    deadline = time.monotonic() + timeout
    detail(latest)
    try:
        while True:
            try:
                event = events.get(timeout=max(0, deadline-time.monotonic()))
            except queue.Empty:
                raise Failure(f'模型验证超时；最后状态：{latest}。') from None
            if event is None:
                break
            kind = event.get('type')
            if kind == 'turn.started':
                latest = '请求已开始，等待 KAIZO 返回模型回复'
            elif kind in ('error', 'turn.failed'):
                latest = error_detail(event)
                failed |= kind == 'turn.failed'
            elif kind == 'item.completed' and event.get('item', {}).get('type') == 'agent_message':
                replies.append(event['item'].get('text', ''))
                latest = '已收到回复，正在校验完成状态'
            elif kind == 'turn.completed':
                completed = True
            detail(latest)
        proc.wait(timeout=5)
        if proc.returncode != 0 or failed or not completed or not replies or replies[-1].strip() != 'KAIZO_READY':
            raise Failure('模型验证未通过；最后状态：' + latest)
    finally:
        stop_child(proc)


def paths_and_checks():
    if os.name != 'nt' or sys.getwindowsversion().major < 10:
        raise Failure('请在 Windows 10 / 11 的原生 Windows 环境运行。')
    if ctypes.windll.shell32.IsUserAnAdmin():
        raise Failure('请关闭此窗口，以普通用户双击 Setup.cmd；不要以管理员身份运行。')
    if os.environ.get('CC_SWITCH_TEST_HOME'):
        raise Failure('检测到 CC_SWITCH_TEST_HOME 覆盖，无法确认真实配置目录。')
    home = Path(ps("[Environment]::GetFolderPath('UserProfile')"))
    codex = Path(os.path.abspath(os.path.expanduser(os.environ.get('CODEX_HOME', str(home / '.codex')))))
    cc = home / '.cc-switch'
    if str(codex).startswith('\\\\'):
        raise Failure('本包配置 Windows 原生 Codex，不写 WSL 或网络共享目录。')
    for path in (home, codex, cc):
        plain_path(path)
    app_paths = Path(os.environ['APPDATA']) / 'com.ccswitch.desktop/app_paths.json'
    if app_paths.exists():
        override = json.loads(app_paths.read_text(encoding='utf-8-sig')).get('app_config_dir_override')
        if override and os.path.normcase(os.path.abspath(override)) != os.path.normcase(str(cc)):
            raise Failure('CC Switch 使用自定义数据目录，请先切回默认目录。')
    settings_path = cc / 'settings.json'
    if settings_path.exists():
        settings = json.loads(settings_path.read_text(encoding='utf-8-sig'))
        override = settings.get('codexConfigDir')
        if override and os.path.normcase(os.path.abspath(os.path.expanduser(override))) != os.path.normcase(str(codex)):
            raise Failure('CC Switch 与 Codex 的配置目录不一致。')
    arch = 'arm64' if 'ARM64' in (os.environ.get('PROCESSOR_ARCHITEW6432', '') + os.environ.get('PROCESSOR_ARCHITECTURE', '')).upper() else 'x64'
    return home, codex, cc, arch


def launch_apps(app, cc_app, detail):
    # Directly launch the registered package executable from the cleaned environment.
    options = {'stdin': subprocess.DEVNULL, 'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL,
               'creationflags': 0x00000008 | 0x00000200}
    start_process([app['app']], cwd=str(Path(app['app']).parent), **options)
    deadline = time.monotonic() + 20
    expected = os.path.normcase(app['app'])
    while time.monotonic() < deadline:
        if any(os.path.normcase(item.get('Path') or '') == expected for item in processes()):
            break
        time.sleep(1)
    else:
        raise Failure('配置和模型验证通过，但未确认桌面应用启动；请从开始菜单打开。')
    start_process([cc_app], **options)
    detail('已启动 ChatGPT / Codex 和 CC Switch')


def restore_mode(home, codex, cc):
    value = input('粘贴此前显示的备份目录：').strip().strip('"')
    directory = Path(value).resolve()
    backup_root = (home / '.kaizo-setup-backups').resolve()
    if backup_root not in directory.parents:
        raise Failure('只能恢复当前用户的 KAIZO 配置备份。')
    data = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    documents = Path(ps("[Environment]::GetFolderPath('MyDocuments')"))
    allowed = {codex / name for name in ('config.toml', 'auth.json', 'AGENTS.md', 'AGENTS.override.md', '.env')}
    allowed |= {cc / name for name in ('settings.json', 'cc-switch.db')}
    allowed |= {documents / sub / name for sub in ('PowerShell','WindowsPowerShell') for name in ('profile.ps1','Microsoft.PowerShell_profile.ps1')}
    if any(Path(entry['path']) not in allowed or not re.fullmatch(r'\d+', entry['slot']) for entry in data['files']):
        raise Failure('备份清单包含不支持的目标。')
    if any(item['name'].upper() not in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY') or item['type'] not in (1,2) for item in data.get('registry', [])):
        raise Failure('备份的环境变量清单不受支持。')
    wait_closed(print)
    backup = Backup.__new__(Backup)
    backup.directory, backup.entries, backup.registry = directory, data['files'], data.get('registry', [])
    backup.restore()
    print('✓ 已恢复备份。恢复的旧代理也会恢复；请按需检查网络。')


def main(mode):
    if mode == 'demo':
        ui = Dashboard()
        ui.animated = False
        ui.states = ['done']*4 + ['running','pending','pending']
        ui.started[4] = time.monotonic()
        ui.details[4] = '保留个人登录，添加 KAIZO 与账户切换入口'
        ui.render()
        return
    init_log(mode)
    home, codex, cc, arch = paths_and_checks()
    LOG.info('TARGET arch=%s codex_directory=%s cc_directory=%s', arch, codex, cc)
    if mode == 'restore':
        restore_mode(home, codex, cc)
        LOG.info('RUN_DONE mode=restore')
        return
    key = load_provider()
    agents = (ROOT / 'AGENTS.md').read_text(encoding='utf-8-sig')
    print('\nKAIZO / Windows 配置\nGPT-6 Astra · Medium · Fast OFF\n' + BASE)
    print('将备份配置，处理确认失效的代理，保留个人登录并添加 KAIZO，写入通用工作规范。')
    print('验证会发送一次简短模型请求，消耗少量 API 额度。请先保存工作并退出相关应用。')
    input('按 Enter 开始，或 Ctrl+C 退出：')
    ui = Dashboard()
    backup = None
    validated = False
    ui.start()
    try:
        with tempfile.TemporaryDirectory(prefix='kaizo-setup-') as temporary:
            work = Path(temporary)
            with ui.step(0, '核对运行环境、持久代理和正在运行的应用'):
                wait_closed(lambda text: ui.detail(0, text))
                backup_root = home / '.kaizo-setup-backups'
                backup_root.mkdir(exist_ok=True)
                plain_path(backup_root)
                backup = Backup(backup_root / (time.strftime('%Y%m%d-%H%M%S') + '-windows-' + str(os.getpid())))
                for name in ('config.toml','auth.json','AGENTS.md','AGENTS.override.md'):
                    backup.capture(codex / name)
                proxy_preflight(home, codex, backup, lambda text: ui.detail(0, text))
            with ui.step(1, '已安装则复用；缺失时安装官方 Windows 应用'):
                app = prepare_codex(work, arch, mode == 'repair', lambda text: ui.detail(1, text))
            with ui.step(2, '检查 CC Switch 版本与配置数据库'):
                cc_app = prepare_cc(arch, mode == 'repair', lambda text: ui.detail(2, text))
                if mode == 'repair':
                    check_db(cc / 'cc-switch.db')
                else:
                    initialize_cc(cc_app, cc, backup, lambda text: ui.detail(2, text))
            with ui.step(3, '备份原登录、模型设置、工作规范和 CC Switch 数据库'):
                wait_closed(lambda text: ui.detail(3, text))
                check_db(cc / 'cc-switch.db')
                for name in ('config.toml','auth.json','AGENTS.md','AGENTS.override.md'):
                    backup.capture(codex / name)
                backup.capture(cc / 'settings.json')
                backup.capture(cc / 'cc-switch.db', database=True)
            with ui.step(4, '保留已有账户；添加 KAIZO 与个人账户切换入口'):
                auth_store, account = configure(app['binary'], work, codex, cc, key, agents)
            with ui.step(5, '用新的验证进程核对登录状态和生效配置'):
                with Rpc(app['binary'], work) as rpc:
                    verify_config(rpc.call('config/read', {'includeLayers': False})['config'], key, auth_store)
                    verify_account(rpc, account)
                smoke_test(app['binary'], work, lambda text: ui.detail(5, text))
            validated = True
            with ui.step(6, '启动应用供新建本地任务使用'):
                launch_apps(app, cc_app, lambda text: ui.detail(6, text))
        ui.stop()
        print('\n✓ 登录状态、模型配置和实际回复验证通过，应用已启动。')
        print('GPT-6 Astra · Medium · Fast OFF · 通用 AGENTS.md')
        print('请新建 Windows 本地 Codex 任务；已有任务可能保留旧设置。')
        print('以后在 CC Switch 的 Codex 页面切换「我的 ChatGPT 账户」与「KAIZO · Astra / Medium」。')
        print('切换前保存工作并退出 Codex，切换后重新打开；尚未登录个人账户时，首次切回需要登录。')
        print('备份：' + str(backup.directory))
        LOG.info('RUN_DONE result=success')
        if LOG_PATH:
            print('完整日志：' + str(LOG_PATH))
    except BaseException as error:
        ui.stop()
        failed = next((i for i, state in enumerate(ui.states) if state == 'failed'), None)
        stage = f'{failed+1:02} {STEPS[failed]}' if failed is not None else '结束处理'
        if not isinstance(error, KeyboardInterrupt):
            report_failure(error, stage, backup.directory if backup else None)
        if backup and not validated:
            try:
                apps_running = bool(processes())
            except Exception:
                apps_running = True
            if not apps_running:
                try:
                    backup.restore(preserve_personal_auth=True)
                    print('↶ 本次配置与代理修改已恢复。')
                except Exception as restore_error:
                    LOG.error('ROLLBACK_FAILED %s', describe_error(restore_error))
                    print('! 自动恢复未完成：' + describe_error(restore_error) + '；请关闭应用后运行 Restore.cmd。')
            else:
                LOG.warning('ROLLBACK_SKIPPED related applications are still running or process status is unavailable')
                print('! 应用仍在运行，未强行覆盖配置；关闭应用后可运行 Restore.cmd。')
            print('备份：' + str(backup.directory))
        raise
    finally:
        ui.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('setup','repair','restore','demo'), nargs='?', default='setup')
    try:
        main(parser.parse_args().mode)
    except KeyboardInterrupt:
        LOG.warning('RUN_CANCELLED')
        print('\n已取消。')
        sys.exit(130)
    except Exception as error:
        if not getattr(error, 'kaizo_reported', False):
            report_failure(error)
        sys.exit(1)
    finally:
        close_log()
