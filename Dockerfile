FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY profile_os ./profile_os
ENV PROFILE_OS_DATA_DIR=/app/data
EXPOSE 8000 8080
CMD ["uvicorn", "profile_os.api:app", "--host", "0.0.0.0", "--port", "8000"]
