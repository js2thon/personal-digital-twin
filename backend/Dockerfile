FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    "fastapi>=0.115" "uvicorn[standard]>=0.30" "pydantic>=2.8" "pydantic-settings>=2.4" \
    "sqlalchemy>=2.0" "psycopg[binary,pool]>=3.2" "celery[redis]>=5.4" "redis>=5.0" \
    "minio>=7.2" "httpx>=0.27" "langgraph>=0.2" "langchain-core>=0.3" "python-multipart>=0.0.9"

COPY app ./app

EXPOSE 8100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
