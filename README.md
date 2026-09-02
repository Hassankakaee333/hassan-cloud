# Hassan Cloud

Standalone Hassan backend — **not** Hassan Desktop.

## Quick start

```bash
cd hassan-cloud
pip install -r requirements.txt
set HASSAN_API_TOKEN=dev-token-change-me
python -m uvicorn hassan_cloud.main:app --host 0.0.0.0 --port 8787
```

## Endpoints

- `GET /v1/health`
- `POST /v1/auth/verify`
- `POST /v1/chat`
- `GET/POST /v1/projects`
- `GET/POST /v1/jobs`, `GET /v1/jobs/{id}`
- `GET/POST /v1/artifacts`
- `POST /v1/agents/run`
- `POST /v1/radar/scan`

## Deploy on Render (stable URL)

1. Push `hassan-cloud/` to GitHub
2. [render.com](https://render.com) → New → Blueprint → connect repo
3. Use `render.yaml` — token is auto-generated as `HASSAN_API_TOKEN`
4. Copy service URL + token into Hassan Android Settings

## Quick public tunnel (testing)

```bash
# terminal 1
set HASSAN_API_TOKEN=hassan-phone-token-2026
python -m uvicorn hassan_cloud.main:app --host 0.0.0.0 --port 8787

# terminal 2 (phone via USB — الأفضل للاختبار الفوري)
adb reverse tcp:8787 tcp:8787
# في التطبيق: http://127.0.0.1:8787
```

```bash
# أو cloudflared / localtunnel (رابط عام مؤقت)
cloudflared tunnel --url http://127.0.0.1:8787
```

## Android settings

- Hassan Cloud URL: `https://your-service.onrender.com` (or tunnel URL)
- Token: value of `HASSAN_API_TOKEN`
- Provider: `auto`
