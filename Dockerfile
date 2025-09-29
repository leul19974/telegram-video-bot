FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Don’t hardcode token here, let Koyeb inject it
CMD ["python", "bot.py"]
