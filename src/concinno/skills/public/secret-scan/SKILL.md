---
name: concinno-secret-scan
description: Basename + word-boundary regex secret detector for .env, credentials, id_rsa, .pypirc. Test-dir + pytest-prefix whitelist avoids false positives on test fixtures.
triggers: [secret, credentials, env, api key, ssh key]
user-invocable: false
license: Apache-2.0
upstream: https://github.com/aiking931931/concinno
---

# SecretScanGuard

Pre-stage filter for auto_commit: scans staged paths against secret patterns, calls git reset HEAD on matches before commit. Never deletes the file. Recognises .env*, credentials*, *api_key*, id_rsa, *.pem, .pypirc. Whitelists tests/, __tests__/, test_*.py.

## Install

```bash
pip install concinno
concinno init
```

## See also

Source: [src/concinno/guards/](https://github.com/aiking931931/concinno/tree/main/src/concinno/guards)
