"""Tombstone left at the old n8n data volume after Bondok's store moved to /var/lib/bondok/pipeline.

Any call from n8n (helper.py <base64 payload>) gets a refusal. It opens no database and touches no file, so
nothing in the n8n container can read or change Bondok's state through it.
"""
import json
import sys

print(json.dumps({'ok': False, 'retired': True, 'kind': 'retired', 'work': [],
                  'error': 'Bondok runs on its own host service; n8n has no Bondok work and no Bondok data'}))
sys.exit(0)
