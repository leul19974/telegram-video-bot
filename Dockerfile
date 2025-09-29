# Use official Python image
FROM python:3.11-slim

# Set work directory
WORKDIR /app

# Copy code
COPY . .

# Install dependencies
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Set environment variable (can also set in Railway settings)
ENV TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN

# Run bot
CMD ["python", "bot.py"]
