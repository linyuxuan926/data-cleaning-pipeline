"""Read-only local Compose backup. Quiesce submissions first; no volumes are removed."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project', default='data-cleaning-pipeline')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    target = args.output or ROOT / '.test-output' / ('backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    target.mkdir(parents=True, exist_ok=False)
    artifacts = {}
    for name, command in (
        ('mysql.sql', ['mysql', 'sh', '-c', 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -u root --single-transaction --routines --triggers --no-tablespaces cleaning']),
        ('minio.tar.gz', ['minio', 'tar', '-czf', '-', '-C', '/data', '.']),
    ):
        path = target / name
        with path.open('wb') as stream:
            result = subprocess.run(['docker', 'compose', '-p', args.project, 'exec', '-T', *command],
                                    cwd=ROOT, stdout=stream, stderr=subprocess.PIPE, timeout=300)
        if result.returncode:
            raise RuntimeError(f'{name} backup failed (exit {result.returncode}); partial output retained for inspection')
        digest = hashlib.file_digest(path.open('rb'), 'sha256').hexdigest()
        artifacts[name] = {'bytes': path.stat().st_size, 'sha256': digest}
    # Preserve secrets locally without displaying their values. This backup is ignored by Git.
    env = target / 'deployment.env'
    env.write_bytes((ROOT / '.env').read_bytes())
    artifacts[env.name] = {'bytes': env.stat().st_size, 'containsSecrets': True}
    manifest = {'project': args.project, 'createdAt': datetime.now(timezone.utc).isoformat(),
                'artifacts': artifacts,
                'note': 'Read-only logical DB and object-volume backups. Quiesce active uploads/tasks before using as a coordinated restore point.'}
    (target / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(f'Backup saved in {target}; no credentials printed and no objects or volumes deleted.')


if __name__ == '__main__':
    main()
