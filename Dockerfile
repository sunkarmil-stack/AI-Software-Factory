# Production image. Built ONLY by trusted CI; deployed by digest. Not built in the reference environment (no daemon).
FROM python:3.12-slim AS build
WORKDIR /src
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt "psycopg[binary]>=3.2"
COPY factory ./factory
COPY catalog ./catalog
COPY migrations ./migrations

FROM python:3.12-slim
RUN useradd --uid 10001 --create-home factory
COPY --from=build /install /usr/local
COPY --from=build /src /app
WORKDIR /app
USER 10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "factory.cli"]
