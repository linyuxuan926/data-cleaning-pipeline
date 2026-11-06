FROM python:3.11.10-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && pip check \
    && useradd --create-home --uid 10001 app
COPY cleaning ./cleaning
USER app
EXPOSE 8000
CMD ["uvicorn", "cleaning.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--limit-concurrency", "32"]
