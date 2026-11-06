"""Add real disk-engine sample tasks to the existing local review account."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from smoke_test import Client

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://localhost:8088')
    args = parser.parse_args()
    if urlparse(args.base_url).hostname not in {'localhost', '127.0.0.1', '::1'}:
        parser.error('Sample-account provisioning is limited to localhost')
    credentials = json.loads((ROOT / '.test-output/local-demo.json').read_text(encoding='utf-8'))
    auth = Client(args.base_url).request('POST', '/api/auth/login', {
        'username': credentials['username'], 'password': credentials['password'],
    })
    owner = Client(args.base_url, auth['token'])
    config = json.loads((ROOT / 'samples/v3-disk-rules.json').read_text(encoding='utf-8'))
    rule = owner.request('POST', '/api/rule-sets', {**config, 'name': 'V3 磁盘订单规则（已运行）'})
    bindings = json.loads((ROOT / 'docs/v3/examples/fixture.json').read_text(encoding='utf-8'))['fieldBindings']
    evidence = {'username': credentials['username'], 'baseUrl': args.base_url, 'ruleSetId': rule['id'], 'tasks': []}
    for fmt in ('csv', 'xlsx', 'parquet'):
        file_id = owner.upload('v3-disk-orders.' + fmt, (ROOT / f'samples/v3-disk-orders.{fmt}').read_bytes())
        task = owner.request('POST', '/api/tasks', {
            'fileId': file_id, 'ruleSetId': rule['id'], 'ruleVersion': rule['version'],
            'schemaVersion': 3, 'fieldBindings': bindings, 'outputFormat': 'csv',
        })
        final = owner.wait(task['id'], 240)
        assert final['status'] == 'SUCCEEDED', final
        report = owner.request('GET', f'/api/tasks/{task["id"]}/report')
        assert (report['inputRows'], report['outputRows'], report['validOutputRows'],
                report['flaggedOutputRows'], report['finalRemovedRows']) == (8, 4, 3, 1, 4), report
        evidence['tasks'].append({'inputFormat': fmt, 'fileId': file_id, 'taskId': task['id'],
                                  'attemptId': task['attemptId'], 'inputRows': 8, 'outputRows': 4})
    (ROOT / '.test-output/local-disk-demo.json').write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print('PASS: three actual disk sample tasks added to the existing local review account; '
          'original credentials and sample tasks preserved. IDs: .test-output/local-disk-demo.json')


if __name__ == '__main__':
    main()
