---
name: security-hardening
description: Security checklist and hardening — OWASP, threat modeling, dependency audit. Triggers on "security", "安全", "hardening", "OWASP", "threat model", "vulnerability", "CVE".
user-invocable: true
disable-model-invocation: true
---

# /security-hardening — Security Hardening Checklist

I secure systems by assuming breach. Defense in depth, not security theater.

> **You MUST** validate all external input at the boundary.
> **You MUST** use parameterized queries — never string concatenation for SQL.
> **You MUST** audit dependencies monthly (npm audit / pip-audit / cargo-audit).

## OWASP Top 10 Quick Reference

| # | Risk | Defense |
|---|------|---------|
| A01 | Broken Access Control | RBAC + row-level checks + default deny |
| A02 | Cryptographic Failures | TLS 1.3, AES-256, bcrypt/argon2 for passwords |
| A03 | Injection | Parameterized queries, input validation |
| A04 | Insecure Design | Threat model before coding |
| A05 | Security Misconfiguration | Hardened defaults, no debug in prod |
| A06 | Vulnerable Components | Automated dep scanning, pin versions |
| A07 | Auth Failures | MFA, rate limiting, session timeout |
| A08 | Data Integrity | Signed artifacts, SBOM, CI verification |
| A09 | Logging Failures | Structured logs, audit trail, tamper-proof |
| A10 | SSRF | Allowlist outbound, no user-controlled URLs |

## Threat Modeling (STRIDE)

1. **S**poofing → Authentication
2. **T**ampering → Integrity checks
3. **R**epudiation → Audit logging
4. **I**nformation Disclosure → Encryption + ACL
5. **D**enial of Service → Rate limiting + scaling
6. **E**levation of Privilege → Least privilege + RBAC
