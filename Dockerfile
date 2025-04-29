FROM python:3.11-slim
RUN pip install --no-cache-dir -r requirements.txt
EXPOSE 80
CMD ["uvicorn", "llama:app", "--host", "0.0.0.0", "--port", "80"]