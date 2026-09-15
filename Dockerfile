FROM python:3.12-slim

# ffmpeg потрібен лише для конвертації TTS-відповіді у OGG/Opus (голосові повідомлення)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]