O (Observable) — debuggable in prod. Required for any change that
runs in production. N/A for tests / scripts / docs:
  - logs at error path with enough context to debug
  - metrics for things that can degrade silently
  - error tracking (sentry / equivalent) hookup
  ✓ evidence: "logger.error('foo failed: %s', exc) at except branch"
  ✗ evidence: "silent except: pass on the network call"
