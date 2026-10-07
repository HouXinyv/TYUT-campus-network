#!/usr/bin/env python3
"""TYUT portal client. Never log credentials, request URLs, or response bodies."""
import argparse
import base64
import fcntl
import getpass
import gzip
import ipaddress
import json
import os
from pathlib import Path
import re
import resource
import socket
import ssl
import urllib.error
import stat
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

BASE = Path.home() / '.local/share/tyut-autologin'
CREDENTIALS = BASE / 'credentials.json'
ROOT = 'https://drcom.tyut.edu.cn/'
API = 'https://drcom.tyut.edu.cn:804/eportal/portal/'
MARKER = '# tyut-autologin managed'
MAX_FAILURES = 3
RETRY_PAUSE_SECONDS = 30 * 60
# Verified campus portal IP. Resolve only this hostname locally while preserving
# the hostname in HTTPS Host, SNI and certificate verification. No system DNS edits.
PORTAL_IP = '219.226.127.250'
_SYSTEM_GETADDRINFO = socket.getaddrinfo

def portal_getaddrinfo(host, port, *args, **kwargs):
    if host == 'drcom.tyut.edu.cn':
        host = PORTAL_IP
    return _SYSTEM_GETADDRINFO(host, port, *args, **kwargs)

socket.getaddrinfo = portal_getaddrinfo

def error_category(exc):
    reason = getattr(exc, 'reason', exc)
    if isinstance(reason, socket.gaierror):
        return 'DNS_FAILURE'
    if isinstance(reason, ssl.SSLError):
        return 'TLS_FAILURE'
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'NETWORK_TIMEOUT'
    if isinstance(exc, urllib.error.HTTPError):
        return 'HTTP_STATUS_' + str(exc.code)
    if isinstance(reason, OSError):
        return 'NETWORK_ERROR_' + str(reason.errno)
    return type(exc).__name__


class SafeError(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SafeError('HTTP redirect refused')

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

def prepare():
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    # Reduce access by other processes of the same UID; root remains privileged.
    import ctypes
    ctypes.CDLL(None).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE
    BASE.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = BASE.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise SafeError('Unsafe application directory')
    BASE.chmod(0o700)

def write_json(path, data):
    fd, tmp = tempfile.mkstemp(dir=BASE)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

def read_credentials():
    fd = os.open(CREDENTIALS, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise SafeError('Credential file must be owned by you with mode 600')
        data = json.load(f)
    if not isinstance(data.get('username'), str) or not isinstance(data.get('password'), str) or not data['username'] or not data['password']:
        raise SafeError('Invalid credential configuration')
    return data

def fetch(url):
    path = urllib.parse.urlsplit(url).path
    label = {'/drcom/chkstatus': 'status', '/a41.js': 'frontend',
             '/eportal/portal/page/loadConfig': 'config', '/eportal/portal/login': 'login',
             '/eportal/portal/mac/unbind': 'unbind', '/': 'portal_home'}.get(path, 'asset')
    started = time.monotonic()
    try:
        result = _fetch(url)
        report('HTTP_OK stage=' + label + ' elapsed_ms=' + str(round((time.monotonic()-started)*1000)))
        return result
    except Exception as exc:
        report('HTTP_FAILED stage=' + label + ' category=' + error_category(exc) + ' elapsed_ms=' + str(round((time.monotonic()-started)*1000)))
        raise

def _fetch(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) TYUT-Autologin/1.0'})
    with OPENER.open(req, timeout=12) as r:
        data = r.read(2_000_001)
        if len(data) > 2_000_000:
            raise SafeError('Response too large')
        if data[:2] == b'\x1f\x8b':
            data = gzip.decompress(data)
        try:
            return data.decode('utf-8')
        except UnicodeDecodeError:
            return data.decode('gb18030')

def jsonp(text):
    m = re.fullmatch(r'\s*(?:[A-Za-z_][\w]*\s*\()?\s*(\{.*\})\s*\)?\s*;?\s*', text, re.S)
    if not m:
        raise SafeError('Unexpected portal response')
    data = json.loads(m.group(1))
    if not isinstance(data, dict):
        raise SafeError('Unexpected portal data')
    return data

def status():
    return jsonp(fetch(ROOT + 'drcom/chkstatus?callback=tyutStatus&v=' + str(int(time.time()))))

def encode(value, key):
    # JavaScript charCodeAt uses UTF-16 code units.
    raw = str(value).encode('utf-16-le')
    return ''.join(format(int.from_bytes(raw[i:i+2], 'little') ^ key, '02x') for i in range(0, len(raw), 2))

def api(path, data, key):
    data = dict(data, callback='tyutCallback', jsVersion='4.X')
    values = {k: encode(v, key) for k, v in data.items()}
    values.update(encrypt=1, lang='zh', v=int(time.time()))
    return jsonp(fetch(API + path + '?' + urllib.parse.urlencode(values)))

def portal_context(current):
    html = fetch(ROOT)
    js = fetch(ROOT + 'a41.js')
    def variable(source, name, default=None):
        m = re.search(r'\b' + re.escape(name) + r'\s*=\s*[\'"]([^\'"]*)[\'"]', source)
        return m.group(1) if m else default
    if variable(js, 'page_data_encrypt') != '1' or variable(js, 'encryption_type') != '1':
        raise SafeError('Portal encoding changed; review required')
    secret = variable(js, 'secret_key')
    if not secret:
        raise SafeError('Portal encoding configuration missing')
    key = 0
    for ch in secret:
        key ^= ord(ch)
    ip = current.get('ss5') or variable(html, 'v4ip') or variable(html, 'ss5')
    try:
        addr = ipaddress.IPv4Address(ip)
        if addr.is_unspecified or addr.is_loopback:
            raise ValueError()
    except Exception:
        raise SafeError('Cannot determine portal client IP')
    mac = re.sub('[:-]', '', current.get('ss4') or variable(html, 'ss4', '000000000000'))
    b64 = lambda s: base64.b64encode(s.encode()).decode()
    params = dict(program_index='', wlan_vlan_id=1, wlan_user_ip=b64(ip), wlan_user_ipv6=b64(''),
                  wlan_user_ssid='', wlan_user_areaid='', wlan_ac_ip='', wlan_ap_mac='000000000000', gw_id='000000000000')
    config = api('page/loadConfig', params, key)
    if config.get('code') != 1 or int(config['data'].get('login_method', -1)) != 1:
        raise SafeError('Unsupported portal configuration')
    config = config['data']
    template_path = '/'.join(urllib.parse.quote(str(config[k]), safe='') for k in ['program_index', 'page_index'])
    template = fetch('https://drcom.tyut.edu.cn:804/eportal/extern/' + template_path + '/pc.js')
    inputs = re.findall(r'<input\b[^>]*>', template, re.I)
    for tag in inputs:
        if re.search(r'name=[\'"]captcha[\'"]', tag) and not re.search(r'display\s*:\s*none', tag):
            raise SafeError('Portal requires interactive captcha')
    if 'checkIOMode' in template:
        raise SafeError('Portal access-mode selection requires review')
    return key, ip, mac, config

def report(message):
    # Only controlled strings. Never include server messages or exception text.
    line = time.strftime('%Y-%m-%d %H:%M:%S %z') + ' ' + message
    print(line)
    logfile = BASE / 'events.log'
    if logfile.exists() and logfile.stat().st_size > 512_000:
        os.replace(logfile, BASE / 'events.log.1')
    fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as f:
        f.write(line + '\n')

def once():
    report('RUN_START')
    statefile = BASE / 'retry.json'
    state = json.loads(statefile.read_text()) if statefile.exists() else {}
    current = status()
    if str(current.get('result')) == '1':
        if state.get('failures') or state.get('last_attempt'):
            write_json(statefile, {})
            report('ONLINE: cleared stale retry state; no login attempted; credential file not opened.')
        else:
            report('ONLINE: no login attempted; credential file not opened.')
        return 0
    if str(current.get('result')) != '0':
        raise SafeError('Unknown online state; no login attempted')
    if not CREDENTIALS.exists():
        report('OFFLINE: credentials not configured')
        return 2
    if state.get('failures', 0) >= MAX_FAILURES:
        elapsed = time.time() - state.get('last_attempt', 0)
        if elapsed < RETRY_PAUSE_SECONDS:
            remaining = max(1, int(RETRY_PAUSE_SECONDS - elapsed))
            report('PAUSED: retry cooldown active; retry_after_seconds=' + str(remaining))
            return 2
        state = {}
        report('RETRY_COOLDOWN_EXPIRED: resuming authentication attempts')
    if time.time() - state.get('last_attempt', 0) < 300:
        return 0
    report('OFFLINE_CONFIRMED; loading portal configuration')
    key, ip, mac, cfg = portal_context(current)
    report('CONFIG_OK; preparing authentication')
    credentials = read_credentials()
    prefix = ',0,' if int(cfg.get('account_prefix', 0)) else ''
    data = dict(login_method=1, user_account=prefix + credentials['username'] + cfg.get('account_suffix', ''),
                user_password=credentials['password'], wlan_user_ip=ip, wlan_user_ipv6='', wlan_user_mac=mac,
                wlan_ac_ip='', wlan_ac_name='', mac_type=0, authex_enable='', web=0,
                terminal_type=1, lang='zh-cn', user_agent='Mozilla/5.0 (X11; Linux x86_64)', enable_r3=cfg.get('enable_r3', 0))
    write_json(statefile, dict(last_attempt=time.time(), failures=state.get('failures', 0) + 1))
    report('LOGIN_SUBMIT')
    result = api('login', data, key)
    summary = {}
    for field in ('result', 'code', 'ret_code', 'error_code'):
        value = result.get(field)
        if isinstance(value, (int, bool)) or (isinstance(value, str) and re.fullmatch(r'-?\d{1,8}|ok|fail', value)):
            summary[field] = value
    report('LOGIN_RESPONSE_CODES ' + json.dumps(summary))
    # Do not print result: server errors may echo credentials.
    for delay in (2, 4, 6):
        time.sleep(delay)
        if str(status().get('result')) == '1':
            write_json(statefile, {})
            report('LOGIN_OK: online state confirmed')
            return 0
    report('LOGIN_UNCONFIRMED: retry delayed; raw response intentionally omitted')
    return 2

def cron(enable):
    old = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
    if old.returncode and 'no crontab' not in old.stderr.lower():
        raise SafeError('Cannot read user crontab')
    lines = [line for line in old.stdout.splitlines() if MARKER not in line]
    if enable:
        import shlex
        lines.append('*/2 * * * * /usr/bin/python3 ' + shlex.quote(str(BASE / 'autologin.py')) + ' run >/dev/null 2>&1 ' + MARKER)
    new = '\n'.join(lines) + '\n'
    completed = subprocess.run(['crontab', '-'], input=new, text=True, capture_output=True)
    if completed.returncode:
        raise SafeError('Cannot install user crontab')
    print('Automatic check every 2 minutes enabled.' if enable else 'Automatic checks disabled.')

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['setup', 'status', 'inspect', 'run', 'enable', 'disable', 'reset-retries'])
    args = parser.parse_args()
    prepare()
    if args.command == 'setup':
        if not sys.stdin.isatty():
            raise SafeError('Setup requires your interactive SSH terminal')
        username = input('校园网账号（不含自动添加的前后缀）: ').strip()
        password = getpass.getpass('校园网密码（输入不显示）: ')
        confirm = getpass.getpass('再次输入密码（输入不显示）: ')
        if not username or not password or password != confirm:
            raise SafeError('Empty credentials or passwords do not match')
        write_json(CREDENTIALS, dict(username=username, password=password))
        write_json(BASE / 'retry.json', {})
        print('Credentials saved privately (600). No login submitted.')
        return 0
    if args.command in ('enable', 'disable'):
        if args.command == 'enable' and not CREDENTIALS.exists():
            raise SafeError('Run setup first')
        cron(args.command == 'enable')
        return 0
    if args.command == 'reset-retries':
        write_json(BASE / 'retry.json', {})
        print('Retry pause cleared.')
        return 0
    if args.command in ('status', 'inspect'):
        current = status()
        print('ONLINE' if str(current.get('result')) == '1' else 'OFFLINE' if str(current.get('result')) == '0' else 'UNKNOWN')
        if args.command == 'inspect':
            portal_context(current)
            print('Portal configuration and template compatible. No credentials read; no login submitted.')
        return 0
    fd = os.open(BASE / 'run.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return once()

if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('Cancelled.')
        sys.exit(130)
    except Exception as exc:
        # Do not emit exception messages: they may contain credential-bearing URLs.
        message = 'OPERATION_FAILED: ' + error_category(exc)
        try:
            report(message)
        except Exception:
            print(message)
        sys.exit(1)
