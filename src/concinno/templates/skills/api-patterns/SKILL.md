---
name: api-patterns
description: API design patterns — REST, GraphQL, gRPC, versioning, error handling. Triggers on "API design", "API 設計", "REST", "GraphQL", "gRPC", "API versioning", "error response".
user-invocable: true
disable-model-invocation: true
---

# /api-patterns — API Design Patterns

I design APIs that are obvious to use correctly and hard to use incorrectly.

> **You MUST** use consistent error response format across all endpoints.
> **You MUST** version breaking changes — URL prefix (/v2/) or header.
> **You MUST** document rate limits, pagination, and auth in OpenAPI/schema.

## REST Conventions

| Action | Method | Path | Status |
|--------|--------|------|--------|
| List | GET | /resources | 200 |
| Create | POST | /resources | 201 |
| Read | GET | /resources/:id | 200 |
| Update | PUT | /resources/:id | 200 |
| Partial | PATCH | /resources/:id | 200 |
| Delete | DELETE | /resources/:id | 204 |

## Error Format (RFC 7807)

```json
{
  "type": "https://api.example.com/errors/validation",
  "title": "Validation Failed",
  "status": 422,
  "detail": "Field 'email' must be a valid email address",
  "instance": "/resources/123"
}
```

## Pagination

- Cursor-based (default): `?cursor=abc&limit=20` — stable, no skip
- Offset-based (simple): `?offset=0&limit=20` — only for small datasets
- Always return `next_cursor` or `has_more` in response
