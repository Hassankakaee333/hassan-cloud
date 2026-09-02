# Hassan Cloud

Production backend for Hassan Candidate. It does not contain or replace the Android app.

## Production stack

- API: Cloudflare Worker in `worker/`
- Database: Neon PostgreSQL
- Job runtime: GitHub Actions
- Artifacts: GitHub Actions Artifacts
- Public URL: `https://hassan-cloud.hassankakaee333.workers.dev`

`GET /v1/health` reports database, runtime, artifact backend, auth configuration,
and the honest chat status.

## Verify

```bash
python -m pytest -q
cd worker
npm test
npm run typecheck
npx wrangler deploy --dry-run
```

## Deploy Worker

```bash
cd worker
npx wrangler deploy
```

Required Worker secrets are `DATABASE_URL`, `GITHUB_TOKEN`,
`GITHUB_CALLBACK_SECRET`, and `HASSAN_BOOTSTRAP_TOKEN`. Never commit their values.

GitHub Actions requires repository secrets `HASSAN_API_URL` and
`HASSAN_CALLBACK_SECRET`.

## Jobs

- `coding`: builds a sample workspace, runs pytest, and emits diff/report/ZIP evidence.
- `android_build`: builds the isolated `fixtures/android-sample` project and emits a real APK.
- The workflow uploads files before registering metadata and marking the job `COMPLETED`.

## Local reference server

The Python FastAPI implementation remains available for local development and
contract tests. Production uses the Worker/Neon path above.

```bash
pip install -r requirements.txt
set HASSAN_ENV=development
set HASSAN_DEV_TOKEN=replace-me
python -m uvicorn hassan_cloud.main:app --host 0.0.0.0 --port 8787
```

No paid LLM key is configured. R2 is intentionally not enabled because its
checkout/subscription step conflicts with the project's no-card rule.
