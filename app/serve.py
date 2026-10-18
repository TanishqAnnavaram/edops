"""Start the API on $PORT. Hosting platforms (Railway) choose the port; locally it is 8000."""

import os

import uvicorn


def main() -> None:
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))


if __name__ == "__main__":
    main()
