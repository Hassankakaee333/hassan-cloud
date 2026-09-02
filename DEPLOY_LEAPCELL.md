# Hassan Cloud — Leapcell deployment

## Service settings (Leapcell Dashboard → Create Service)

| Field | Value |
|-------|-------|
| Runtime | Python 3.12 |
| Repository | `https://github.com/Hassankakaee333/hassan-cloud` |
| Branch | `main` |
| Build Command | `pip install -r requirements.txt` |
| Start Command | `uvicorn hassan_cloud.main:app --host 0.0.0.0 --port 8080` |
| Port | `8080` |

## Environment variables

```
HASSAN_ENV=production
HASSAN_DB_BACKEND=postgres
HASSAN_JOB_RUNTIME=dispatch
HASSAN_ARTIFACT_STORE=db
DATABASE_URL=<from Leapcell PostgreSQL addon>
HASSAN_BOOTSTRAP_TOKEN=<generate with: python -c "import secrets; print(secrets.token_urlsafe(32))">
```

## PostgreSQL (free tier)

1. Leapcell Dashboard → Databases → Create PostgreSQL (free)
2. Copy connection URL into `DATABASE_URL`
3. Use format: `postgresql://user:pass@host:port/dbname?sslmode=require`

## Health check

Path: `/v1/health`

## Notes

- No payment card required on Leapcell free tier (unlike Render).
- `HASSAN_JOB_RUNTIME=dispatch` enqueues jobs without blocking HTTP.
- Artifact bytes stored in PostgreSQL (`EPHEMERAL_DB` label in health) — metadata durable, suitable for POC.
- Chat LLM remains `NOT_CONFIGURED` without `OPENAI_API_KEY`.
