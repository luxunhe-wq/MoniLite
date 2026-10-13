"""Isolated image check: no real recipients or production account volumes."""
import http.cookiejar
import json
import os
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener


def main():
    image = os.environ.get('MONILITE_TEST_IMAGE', 'monilite:local')
    name = f'monilite-alert-check-{os.getpid()}'
    with tempfile.TemporaryDirectory(prefix='monilite-docker-alerts-') as directory:
        try:
            subprocess.run(['docker', 'run', '-d', '--name', name, '--read-only', '--tmpfs', '/tmp',
                            '-p', '127.0.0.1::8080', '-e', 'MONILITE_DATA_DIR=/var/lib/monilite',
                            '-v', f'{directory}:/var/lib/monilite', image], check=True, capture_output=True)
            address = subprocess.check_output(['docker', 'port', name, '8080/tcp'], text=True).strip()
            client = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
            csrf = None

            def request(path, method='GET', data=None):
                headers = {'Content-Type': 'application/json'}
                if csrf:
                    headers['X-CSRF-Token'] = csrf
                req = Request('http://' + address + path, method=method, headers=headers,
                              data=json.dumps(data).encode() if data is not None else None)
                with client.open(req, timeout=10) as response:
                    return json.load(response)

            def wait_for(predicate):
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    try:
                        value = predicate()
                        if value:
                            return value
                    except (HTTPError, URLError, ConnectionError):
                        pass
                    time.sleep(0.2)
                print('Expected address:', address, flush=True)
                subprocess.run(['docker', 'port', name, '8080/tcp'], check=False)
                subprocess.run(['docker', 'logs', '--tail', '15', name], check=False)
                raise AssertionError('Timed out waiting for disposable alert container')

            wait_for(lambda: request('/api/health')['status'] == 'ok')
            account = request('/api/auth/setup', 'POST', {'username': 'docker_alert_owner', 'password': 'docker-alert-password'})
            csrf = account['csrf_token']
            assert not request('/api/admin/alerts')['config']['enabled']
            metrics = request('/api/metrics')
            assert metrics['memory']['percent'] > 1, 'Need >1% memory use to exercise the real background rule'
            config = {'enabled': True, 'channel': 'webhook', 'credential': 'https://127.0.0.1:9/notify?token=container-secret',
                      'duration_seconds': 0, 'repeat_seconds': 60,
                      'rules': {'cpu': {'enabled': False, 'threshold': 80}, 'memory': {'enabled': True, 'threshold': 1},
                                'disk': {'enabled': False, 'threshold': 90}}}
            saved = request('/api/admin/alerts', 'PATCH', config)
            assert saved['config']['credential_configured']
            assert 'container-secret' not in json.dumps(saved)
            history = wait_for(lambda: request('/api/admin/alerts')['history'])
            assert not history[0]['success']
            assert history[0]['event']['metric'] == 'memory'
            assert 'container-secret' not in json.dumps(history)
            request('/api/admin/alerts', 'PATCH', {'enabled': False, 'duration_seconds': 37})
            subprocess.run(['docker', 'restart', name], check=True, capture_output=True)
            address = subprocess.check_output(['docker', 'port', name, '8080/tcp'], text=True).strip()
            wait_for(lambda: request('/api/health')['status'] == 'ok')
            restored = request('/api/admin/alerts')
            assert restored['config']['duration_seconds'] == 37
            assert restored['config']['credential_configured']
            assert not restored['config']['enabled']
            assert restored['history'][0]['id'] == history[0]['id']
            print('Docker alert check passed: packaged module, background rule/failure record, credential redaction, read-only root and persisted settings/history after restart.')
        finally:
            subprocess.run(['docker', 'rm', '-f', name], check=False, capture_output=True)


if __name__ == '__main__':
    main()
