backend (API endpoint / server route)
  ✓ evidence: pytest/jest output showing the new endpoint test
    passes, AND a curl/httpie/requests call to the actual running
    server with expected status code + body shape.
  ✗ evidence: unit test exists but no integration call against
    a running server; or 401/500 from the live call.
