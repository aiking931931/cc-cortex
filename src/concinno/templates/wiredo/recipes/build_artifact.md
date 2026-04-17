build_artifact (wheel / tarball / docker image)
  ✓ evidence: build success + actual install (`pip install dist/*`
    or `docker pull`) into clean env + smoke test invocation
    showing the artifact works end-to-end.
  ✗ evidence: build succeeded but never installed/loaded fresh.
