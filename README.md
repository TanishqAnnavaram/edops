# edops

Event-driven order processing: transactional outbox, idempotency, saga with compensation.

## Run locally
```
copy .env.example .env
python -m venv C:\webdev\venvs\edops
C:\webdev\venvs\edops\Scripts\activate
pip install -r requirements-dev.txt
docker compose up -d
docker compose ps
pytest
uvicorn app.main:app --reload --port 8000
```
Then open http://localhost:8000/health

`pytest` should fail with `ModuleNotFoundError: app.orders` until you write it. That is correct.

## Schema changes
`db/init.sql` only runs when the Postgres volume is first created. After any change to it, recreate the volume:
```
docker compose down -v
docker compose up -d
```
This deletes all local data.
