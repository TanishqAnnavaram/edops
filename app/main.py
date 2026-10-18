from fastapi import FastAPI

app = FastAPI(title="edops order-api")


@app.get("/health")
def health():
    return {"status": "ok"}


# TODO (yours): POST /orders and GET /orders/{id}
# Hint: the order row and the outbox row must be written inside ONE transaction.
# Before you write it, answer in design.md: what must be true if the process
# dies after the first INSERT and before the second?
