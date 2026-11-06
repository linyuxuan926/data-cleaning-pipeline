"""Check existing accepted tasks after an additive deployment; never modifies them."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from smoke_test import Client

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://localhost:8088')
    parser.add_argument('--before', type=Path, default=ROOT / '.test-output/v3-next-preupgrade.json')
    parser.add_argument('--credentials', type=Path, default=ROOT / '.test-output/local-demo.json')
    args = parser.parse_args()
    credentials = json.loads(args.credentials.read_text(encoding='utf-8'))
    previous = json.loads(args.before.read_text(encoding='utf-8'))
    auth = Client(args.base_url).request('POST', '/api/auth/login', {
        'username': credentials['username'], 'password': credentials['password'],
    })
    assert auth['user']['username'] == previous['username']
    owner = Client(args.base_url, auth['token'])
    checks = []
    for old in previous['tasks']:
        task = owner.request('GET', f'/api/tasks/{old["id"]}')
        for key in ('attemptId', 'engineVersion', 'status'):
            assert task[key] == old[key], (old['id'], key, task[key], old[key])
        report = owner.request('GET', f'/api/tasks/{old["id"]}/report')
        assert report == old['report'], f'Historical report changed: {old["id"]}'
        result = owner.request('GET', f'/api/tasks/{old["id"]}/artifacts/result', raw=True)
        digest = hashlib.sha256(result).hexdigest()
        assert digest == old['resultSha256'], f'Historical artifact changed: {old["id"]}'
        checks.append({'taskId': old['id'], 'attemptId': task['attemptId'], 'engineVersion': task['engineVersion'],
                       'resultSha256': digest, 'reportUnchanged': True, 'originalAccountWorks': True})
    env = dict(line.split('=', 1) for line in (ROOT / '.env').read_text(encoding='utf-8').splitlines()
               if '=' in line and not line.lstrip().startswith('#'))
    admin_auth = Client(args.base_url).request('POST', '/api/auth/login', {
        'username': env['BOOTSTRAP_ADMIN_USERNAME'], 'password': env['BOOTSTRAP_ADMIN_PASSWORD'],
    })
    admin = Client(args.base_url, admin_auth['token'])
    policy = admin.request('GET', '/api/admin/retention/policy')
    assert policy['enabled'] is False and policy['includeExisting'] is False, 'Existing-data protection disabled'
    evidence = {'baseUrl': args.base_url, 'checks': checks, 'retentionEnabled': False, 'includeExisting': False}
    (ROOT / '.test-output/v3-next-upgrade-verification.json').write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'PASS: original account and {len(checks)} accepted tasks accessible; attempt IDs, engines, reports '
          'and downloaded SHA-256 unchanged; retention disabled and existing objects protected.')


if __name__ == '__main__':
    main()
