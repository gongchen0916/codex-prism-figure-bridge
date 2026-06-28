# Security Policy

Do not commit API keys, OAuth tokens, unpublished raw data, patient data, or personal absolute paths.

Recommended local scan before pushing:

```bash
rg -n --hidden -S '(sk-[A-Za-z0-9_-]{20,}|ghp_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|OPENAI_API_KEY|ANTHROPIC_API_KEY|api[_-]?key|secret|password|token|/Users/)' .
```

Generated presentation and vector outputs are intentionally ignored by `.gitignore`.
