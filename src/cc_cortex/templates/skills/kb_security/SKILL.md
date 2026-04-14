---
name: kb_security
description: Security knowledge base — OWASP, supply chain, secrets management, vulnerability patterns. Triggers on "安全知識", "security KB", "OWASP details", "supply chain security", "secrets management".
user-invocable: false
---

# 安全知識庫

> I treat every input as hostile and every secret as a ticking bomb. Security is not a feature — it's a property of every line of code.

> **You MUST** never log secrets, tokens, or PII — even in debug mode.
> **You MUST** use allowlists over denylists for input validation.
> **You MUST** rotate credentials on any suspected leak — no "wait and see".

## 鐵律

1. **Input is hostile** — Validate, sanitize, and escape at every boundary
2. **Least privilege** — Grant minimum permissions, revoke when unused
3. **Defense in depth** — No single layer protects everything
4. **Secrets in vault** — Never in code, env files committed, or logs

## 決策樹

```
Secret storage?
  ├─ Dev → .env (gitignored) + 1Password/Bitwarden
  ├─ CI → GitHub Secrets / Vault
  └─ Prod → Cloud KMS / HashiCorp Vault / AWS Secrets Manager

Dependency risk?
  ├─ New dep → Check: maintainer count, last update, known CVEs
  ├─ Existing dep alert → Patch within 48h (critical) / 7d (high)
  └─ Unmaintained dep → Fork or replace, never ignore
```

## 按需讀取

| 要做什麼 | 讀哪個檔案 |
| --- | --- |
| OWASP 詳細防禦 | `/security-hardening` skill |
| 認證授權模式 | `/auth-patterns` skill |
| 依賴審計 SOP | CCC `dep_audit` guard |
