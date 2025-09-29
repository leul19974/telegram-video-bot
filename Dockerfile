# Use official Python slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy all files
COPY . .

# Upgrade pip and install dependencies
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Set Telegram token as environment variable (also can set in Railway/Koyeb settings)
ENV TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN

# Run the bot
CMD ["python", "bot.py"]
