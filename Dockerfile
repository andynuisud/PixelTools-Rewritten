FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py values.py api.py scanner.py tradebot.py ./

CMD ["python", "tradebot.py"]
