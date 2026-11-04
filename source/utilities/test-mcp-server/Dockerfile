FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt server.py ./
RUN pip install --no-cache-dir -r requirements.txt

USER 1000:1000
EXPOSE 8000
CMD ["python", "server.py"]
