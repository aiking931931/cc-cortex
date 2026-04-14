deploy (live infra push)
  ✓ evidence: deploy log shows success + curl HTTPS check 200 to
    live URL + content match + monitoring metric/log entry showing
    the new version is serving.
  ✗ evidence: deploy.py ran but no live URL check, or 502/503.
