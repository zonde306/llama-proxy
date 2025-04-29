FROM python:3.11-slim
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 8080
CMD ["uvicorn", "llama:app", "--host", "0.0.0.0", "--port", "8080"]