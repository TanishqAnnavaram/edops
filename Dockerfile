FROM python:3.12-slim

WORKDIR /srv

# Requirements before source so code edits don't invalidate the pip layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
# init.sql ships in the image so `python -m app.init_db` can run as a pre-deploy step.
COPY db/ ./db/

RUN useradd --create-home --uid 1000 app
USER app

EXPOSE 8000
# One image for every process; compose and Railway override the command for the workers.
CMD ["python", "-m", "app.serve"]
