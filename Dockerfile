# Hugging Face Space (sdk: docker). Also runs anywhere: docker build -t daily-briefing . && docker run -p 7860:7860 --env-file .env daily-briefing
FROM python:3.12-slim

# Spaces run the container as user 1000; the app writes the synced data, briefings and SQLite files here.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user     PATH=/home/user/.local/bin:$PATH     PYTHONUNBUFFERED=1     HOST=0.0.0.0     PORT=7860
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user . .

EXPOSE 7860
# Data (HF_DATA_URL + HF_TOKEN secrets) is synced from the private bucket on first start.
CMD ["python", "app.py"]
