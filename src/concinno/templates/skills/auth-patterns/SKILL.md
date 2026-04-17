---
name: auth-patterns
description: Authentication and authorization patterns — JWT, OAuth, RBAC, session management. Triggers on "auth", "認證", "授權", "JWT", "OAuth", "RBAC", "session", "login".
user-invocable: true
disable-model-invocation: true
---

# /auth-patterns — Authentication & Authorization Patterns

I design auth that is invisible when it works and impossible to bypass when it matters.

> **You MUST** hash passwords with bcrypt/argon2 — never MD5/SHA.
> **You MUST** use short-lived access tokens (<15min) + refresh tokens.
> **You MUST** validate authorization on every request, server-side.

## Decision Tree

```
Auth type?
  ├─ SPA/Mobile → OAuth2 + PKCE + refresh rotation
  ├─ Server-rendered → HTTP-only secure cookies + CSRF token
  ├─ API-to-API → API keys (low-trust) or mTLS (high-trust)
  ├─ Microservices → JWT propagation + gateway validation
  └─ Third-party login → OIDC (Google/GitHub/Apple)

Authorization model?
  ├─ Simple roles → RBAC (admin/editor/viewer)
  ├─ Fine-grained → ABAC (attribute-based policies)
  ├─ Multi-tenant → Tenant isolation + row-level security
  └─ Complex hierarchy → ReBAC (relationship-based, Zanzibar-style)
```

## Token Security

| Rule | Reason |
|------|--------|
| Store access token in memory only | XSS can't read memory |
| Store refresh token in httpOnly cookie | JS can't access |
| Rotate refresh token on use | Detect token theft |
| Bind token to fingerprint | Prevent token replay |
| Revocation list for logout | Immediate invalidation |
