# AlphaLens API

FastAPI backend serving fund rankings, attribution, and stats from parquet files.

## Local development

```bash
cd api/
pip install -r requirements.txt

# Point at your mf_data directory
export DATA_DIR=../mf_data

uvicorn main:app --reload
```

API runs at http://localhost:8000
Docs at http://localhost:8000/docs

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Health check + data freshness |
| `GET /api/funds` | All ranked active funds (supports ?cat=, ?amc=, ?min_score=, ?search=) |
| `GET /api/funds/{code}` | Single fund detail |
| `GET /api/attribution/{code}` | Monthly attribution history |
| `GET /api/stats` | Summary stats for hero section |
| `GET /api/categories` | Categories with fund counts |
| `GET /api/amcs` | AMCs with fund counts |
| `GET /reload?secret=XXX` | Force cache reload (called by daily pipeline) |

## Deployment on Railway

1. Create a new project on [railway.app](https://railway.app)
2. Connect your GitHub repo
3. Set environment variables:
   - `DATA_DIR` — path to mf_data/ (use a Railway volume)
   - `RELOAD_SECRET` — random secret string
   - `PORT` — Railway sets this automatically
4. Deploy

## Deployment on Render

1. New Web Service → connect GitHub repo
2. Build command: `pip install -r requirements.txt`
3. Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Set same env vars as above

## Daily sync

After the pipeline runs (`run_monthly_update.py`), it automatically calls
`GET /reload?secret=RELOAD_SECRET` to refresh the API cache.

Set these in your environment before running the pipeline:
```bash
export ALPHALENS_API_URL=https://your-api.railway.app
export RELOAD_SECRET=your_secret_here
```

## CORS

Currently allows all origins (`*`). Before going live, restrict to your
frontend domain in `main.py`:
```python
allow_origins=["https://your-frontend.vercel.app"]
```
