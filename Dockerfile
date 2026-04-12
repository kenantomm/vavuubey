FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 7860

ENV PORT=7860
ENV DB_PATH=/data/vxparser.db

CMD ["python", "server.py"]
