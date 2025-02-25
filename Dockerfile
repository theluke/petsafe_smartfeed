FROM python:3.9-slim
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install requirements
COPY bridge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy necessary files
COPY config.yaml.python .
COPY get_tokens.py .
COPY bridge/petsafe_bridge.py bridge/
COPY tokens.json .

# Create log directory
RUN mkdir -p /app/logs

# Expose the port the bridge runs on
EXPOSE 5000

# Command to run the bridge
CMD ["python", "bridge/petsafe_bridge.py"]