"""Codex CLI candidate adapter; no live verification is implied by unit tests.

Interface source: https://developers.openai.com/codex/noninteractive
The child gets a disposable CODEX_HOME (no saved user auth/config), read-only
sandbox and explicit request context. Read access still depends on the CLI's
sandbox; this adapter does not establish file-read isolation. Authentication can be supplied by the
operator through CODEX_API_KEY or explicit custom-provider settings. Custom
providers must support Responses; Chat Completions success is insufficient.
This module never persists or logs credentials.
"""
from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit

from .host import request_digest, request_identity, validate_request

_PROVIDER_KEY_ENV = 'OPENCODING_CODEX_PROVIDER_KEY'


def _contains_secret(value, secret):
    """Inspect decoded JSON strings, including keys, before any serialization."""
    if not secret:
        return False
    if isinstance(value, str):
        return secret in value
    if isinstance(value, dict):
        return any(_contains_secret(key, secret) or _contains_secret(item, secret)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item, secret) for item in value)
    return False


def _provider_settings(base_url, model, api_key):
    """Validate in-memory provider settings without echoing caller values."""
    values = (base_url, model, api_key)
    placeholders = {'placeholder', 'changeme', 'replaceme', 'replace-me', 'your-api-key',
                    'your_api_key', 'your-model', 'your_model', 'api_key', 'api-key',
                    'test-key', 'dummy', 'none', 'null'}
    if any(type(value) is not str or not value.strip() or value != value.strip()
           or any(ord(c) < 33 or ord(c) == 127 for c in value)
           or any(c in value for c in '<>{}') or value.lower() in placeholders
           or 'placeholder' in value.lower()
           or value.lower().startswith(('your-', 'your_', 'replace-', 'replace_', 'sk-your-', 'sk-your_'))
           for value in values):
        raise ValueError('invalid Codex Responses provider settings')
    try:
        parsed = urlsplit(base_url)
        host = parsed.hostname
        parsed.port  # Reject malformed ports without exposing the URL.
        local = host == 'localhost'
        if host and not local:
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (not host or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or '?' in base_url or '#' in base_url
                or '\\' in base_url or '%' in parsed.netloc
                or parsed.scheme not in ('http', 'https')
                or (parsed.scheme == 'http' and not local)
                or host.lower() in {'example.com', 'example.org', 'example.net'}
                or host.lower().endswith(('.example.com', '.example.org', '.example.net', '.invalid'))
                or 'placeholder' in host.lower() or host.lower().startswith(('your-', 'your_'))
                or parsed.path.rstrip('/').endswith(('/chat/completions', '/responses'))):
            raise ValueError()
    except ValueError:
        raise ValueError('invalid Codex Responses provider endpoint') from None
    if len(base_url) > 8192 or len(model) > 256 or not 8 <= len(api_key) <= 16384 or api_key in model:
        raise ValueError('invalid Codex Responses provider settings')
    decoded_url = base_url
    for _ in range(8):
        if api_key in decoded_url:
            raise ValueError('invalid Codex Responses provider endpoint')
        next_url = unquote(decoded_url)
        if next_url == decoded_url:
            break
        decoded_url = next_url
    else:
        # Deeply nested percent escapes are unnecessary for a provider base URL.
        raise ValueError('invalid Codex Responses provider endpoint')
    return base_url.rstrip('/'), model, api_key


def resolve_codex_command():
    """Avoid Windows batch-shell interpretation; use the npm entry with Node."""
    executable = shutil.which('codex')
    if not executable:
        raise FileNotFoundError('Codex CLI unavailable')
    if os.name == 'nt' and Path(executable).suffix.lower() != '.exe':
        script = Path(executable).parent / 'node_modules/@openai/codex/bin/codex.js'
        node = shutil.which('node')
        if not node or not script.is_file():
            raise FileNotFoundError('Codex native or Node entry unavailable')
        return [node, str(script)]
    return [executable]


def _stop_tree(process):
    if os.name == 'nt':
        # Kill descendants even when the launcher is waiting on a child.
        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=5, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)


def _schema(request):
    identity = request_identity(request)
    fields = {}
    for key, value in identity.items():
        if key == 'root':
            # Some Responses gateways truncate backslash-containing enum values.
            # The returned root is still compared exactly with request_identity.
            fields[key] = {'type': 'string'}
        elif type(value) is list:
            # Responses structured outputs reject an enum whose value is an
            # array, even though that is valid JSON Schema. Constrain each
            # item instead; the adapter still performs the exact identity
            # comparison after decoding the response.
            fields[key] = {'type': 'array', 'minItems': len(value),
                           'maxItems': len(value),
                           'items': {'type': 'string', 'enum': value}}
        else:
            fields[key] = {'type': 'integer' if type(value) is int else 'string', 'enum': [value]}
    return {'type': 'object', 'additionalProperties': False,
            'required': ['status', 'request_digest', 'identity', 'files'],
            'properties': {
                'status': {'type': 'string', 'enum': ['succeeded']},
                'request_digest': {'type': 'string', 'enum': [request_digest(request)]},
                'identity': {'type': 'object', 'additionalProperties': False,
                             'required': list(fields), 'properties': fields},
                'files': {'type': 'object', 'additionalProperties': False,
                          'required': request['outputs'],
                          'properties': {p: {'type': 'string'} for p in request['outputs']}}}}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


class CodexCLIHost:
    """Trusted local CLI only. Requires operator authorization before real use."""

    live_verified = False

    def __init__(self, command=None, *, max_output_bytes=2 * 1024 * 1024,
                 base_url=None, model=None, api_key=None):
        if type(max_output_bytes) is not int or max_output_bytes < 1:
            raise ValueError('invalid output limit')
        self.command = list(command) if command is not None else None
        self.max_output_bytes = max_output_bytes
        # Diagnostics stay on the adapter instance so the strict Host response
        # envelope remains unchanged and cannot be mistaken for candidate data.
        self.last_diagnostics = None
        self._provider = None
        if any(value is not None for value in (base_url, model, api_key)):
            self._provider = _provider_settings(base_url, model, api_key)

    @classmethod
    def from_provider_config(cls, config, **kwargs):
        """Use existing openai_compatible settings; does not probe or certify Responses.

        A Chat Completions health check is not evidence of Codex compatibility.
        Configuration is supplied by the caller; this method reads no auth files.
        """
        if (type(config) is not dict or type(config.get('schema_version')) is not int
                or config['schema_version'] != 1 or config.get('provider') != 'openai_compatible'):
            raise ValueError('Codex requires openai_compatible provider configuration')
        base_url, model, api_key = _provider_settings(config.get('base_url'), config.get('model'), config.get('api_key'))
        return cls(base_url=base_url, model=model, api_key=api_key, **kwargs)

    def execute(self, request, cancel_event):
        request = validate_request(request)
        self.last_diagnostics = None
        result = {'status': 'failed', 'request_digest': request_digest(request),
                  'identity': request_identity(request), 'files': {}}
        if cancel_event.is_set():
            self.last_diagnostics = {'code': 'cancelled'}
            result['status'] = 'cancelled'
            return result
        secret = self._provider[2] if self._provider else os.environ.get('CODEX_API_KEY', '')
        if _contains_secret(request, secret):
            self.last_diagnostics = {'code': 'secret_in_request'}
            return result
        process = None
        started = time.monotonic()
        try:
            command = self.command or resolve_codex_command()
            with tempfile.TemporaryDirectory(prefix='opencoding-codex-') as temporary:
                work = Path(temporary)
                # No project files are copied; the CLI still has whatever read access
                # the selected read-only sandbox grants, so this is not a security sandbox.
                schema_path = work / 'response-schema.json'
                schema_path.write_text(json.dumps(_schema(request)), encoding='utf-8')
                prompt = ('Return candidate file contents only, conforming to the supplied schema. '
                          'Do not execute commands, access external files, or modify any files. '
                          'Use only this request as context:\n' + json.dumps(request, ensure_ascii=False))
                input_path = work / 'request.txt'
                input_path.write_text(prompt, encoding='utf-8')
                env = {key: value for key, value in os.environ.items()
                       if key.upper() in {'PATH', 'PATHEXT', 'SYSTEMROOT', 'WINDIR',
                                          'COMSPEC', 'CODEX_API_KEY', 'HTTPS_PROXY',
                                          'HTTP_PROXY', 'NO_PROXY'}}
                for key in ('CODEX_HOME', 'HOME', 'USERPROFILE', 'LOCALAPPDATA', 'APPDATA', 'TMP', 'TEMP'):
                    folder = work / key.lower()
                    folder.mkdir()
                    env[key] = str(folder)
                args = [*command, 'exec', '--sandbox', 'read-only', '--ephemeral',
                        '--ignore-user-config', '--ignore-rules', '--skip-git-repo-check',
                        '--color', 'never', '--cd', str(work), '--output-schema', str(schema_path), '-']
                if self._provider:
                    base_url, model, _ = self._provider
                    env.pop('CODEX_API_KEY', None)
                    env[_PROVIDER_KEY_ENV] = secret
                    # Official custom-provider contract: Responses transport + env_key.
                    # All argv values below are non-secret; credentials remain in env.
                    settings = {'model_provider': 'opencoding_relay', 'model': model,
                                'model_providers.opencoding_relay.name': 'OpenCoding relay',
                                'model_providers.opencoding_relay.base_url': base_url,
                                'model_providers.opencoding_relay.env_key': _PROVIDER_KEY_ENV,
                                'model_providers.opencoding_relay.wire_api': 'responses',
                                'model_providers.opencoding_relay.request_max_retries': 0,
                                'model_providers.opencoding_relay.stream_max_retries': 0}
                    options = [part for key, value in settings.items()
                               for part in ('-c', key + '=' + json.dumps(value))]
                    args[-1:-1] = options
                with input_path.open('rb') as stdin:
                    process = subprocess.Popen(args, cwd=work, env=env, stdin=stdin,
                                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                               start_new_session=os.name != 'nt',
                                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
                    overflow = threading.Event()
                    lock = threading.Lock()
                    total = [0]
                    output = bytearray()
                    def read(stream, retain):
                        try:
                            while True:
                                chunk = stream.read(4096)
                                if not chunk:
                                    break
                                with lock:
                                    total[0] += len(chunk)
                                    if total[0] > self.max_output_bytes:
                                        overflow.set()
                                    elif retain:
                                        output.extend(chunk)
                        finally:
                            stream.close()
                    readers = [threading.Thread(target=read, args=(process.stdout, True), daemon=True),
                               threading.Thread(target=read, args=(process.stderr, False), daemon=True)]
                    for reader in readers:
                        reader.start()
                    while True:
                        if cancel_event.is_set():
                            result['status'] = 'cancelled'
                            self.last_diagnostics = {'code': 'cancelled'}
                            break
                        if time.monotonic() - started >= request['timeout_seconds']:
                            result['status'] = 'timed_out'
                            self.last_diagnostics = {'code': 'timed_out'}
                            break
                        if overflow.is_set():
                            self.last_diagnostics = {'code': 'output_limit_exceeded'}
                            break
                        if process.poll() is not None and not any(r.is_alive() for r in readers):
                            if process.returncode == 0:
                                try:
                                    payload = json.loads(output.decode('utf-8'), object_pairs_hook=_unique_object)
                                except (UnicodeError, ValueError, json.JSONDecodeError):
                                    self.last_diagnostics = {'code': 'invalid_response'}
                                else:
                                    if (type(payload) is dict and set(payload) == set(result)
                                            and not _contains_secret(payload, secret)
                                            and payload['status'] == 'succeeded'
                                            and payload['request_digest'] == result['request_digest']
                                            and request_digest(payload['identity']) == request_digest(result['identity'])
                                            and type(payload['files']) is dict
                                            and set(payload['files']) == set(request['outputs'])
                                            and all(type(v) is str for v in payload['files'].values())):
                                        result = payload
                                        self.last_diagnostics = {'code': 'succeeded'}
                                    else:
                                        self.last_diagnostics = {'code': 'invalid_response'}
                            else:
                                self.last_diagnostics = {
                                    'code': 'process_failed',
                                    'exit_code': process.returncode,
                                }
                            break
                        cancel_event.wait(.01)
                    if process.poll() is None or any(r.is_alive() for r in readers):
                        _stop_tree(process)
                    for reader in readers:
                        reader.join(timeout=1)
        except FileNotFoundError:
            self.last_diagnostics = {'code': 'cli_unavailable'}
            result['status'] = 'failed'
            result['files'] = {}
        except (OSError, subprocess.SubprocessError):
            self.last_diagnostics = {'code': 'process_launch_failed'}
            result['status'] = 'failed'
            result['files'] = {}
        except (ValueError, TypeError):
            self.last_diagnostics = {'code': 'invalid_response'}
            result['status'] = 'failed'
            result['files'] = {}
        finally:
            if process is not None and process.poll() is None:
                try:
                    _stop_tree(process)
                except (OSError, subprocess.SubprocessError):
                    self.last_diagnostics = {'code': 'process_stop_failed'}
                    result['status'] = 'failed'
                    result['files'] = {}
        return result
