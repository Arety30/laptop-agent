# Bot runs on stable Python 3.12 (LangChain wheels are reliable here, unlike the
# host's 3.14). The agents edit files on your laptop via a bind-mount, and run
# shell commands inside this container against those same mounted files.
FROM python:3.12-slim

# Tools the autonomous agents commonly reach for. Add more as you need them.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
