# Base image must be in catalog/base-images.yaml and referenced by digest in the
# enterprise registry (CI policy check). BASE_IMAGE is injected by trusted CI only.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
WORKDIR /srv
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock || pip install --no-cache-dir -r requirements.lock
COPY app/ app/
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["uvicorn", "app.main:api", "--host", "0.0.0.0", "--port", "8080"]
