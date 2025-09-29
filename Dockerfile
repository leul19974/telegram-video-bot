# Use official Python image
FROM python:3.13-slim

# Set working directory
WORKDIR /app

# Copy project files
COPY . /app

# Upgrade pip
RUN pip install --no-cache-dir --upgrade pip

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Set environment variable for Python buffering
ENV PYTHONUNBUFFERED=1

# Run the bot
CMD ["python", "bot.py"]
