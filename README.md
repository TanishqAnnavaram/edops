# edops

Event-driven order processing: transactional outbox, idempotency, saga with compensation.

## Run locally
Requires Python 3.12 and Docker Desktop. If PowerShell refuses to run `activate`, run
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once.
```
copy .env.example .env
py -3.12 -m venv C:\webdev\venvs\edops
C:\webdev\venvs\edops\Scripts\activate
pip install -r requirements-dev.txt
docker compose up -d --build
docker compose ps
pytest
```
Compose runs postgres, rabbitmq and order-api. Then open http://localhost:8000/health

For hot reload outside Docker, use another port so it doesn't collide with the container:
`uvicorn app.main:app --reload --port 8001`

Until `app/orders.py` is implemented, `pytest` passes the health test and fails the four
atomicity tests with `NotImplementedError`. That is correct.

## Schema changes
`db/init.sql` only runs when the Postgres volume is first created. After any change to it, recreate the volume:
```
docker compose down -v
docker compose up -d
```
This deletes all local data.
