# nosemgrep: dockerfile-source-not-pinned
FROM python:3.13-slim
# checkov:skip=CKV_DOCKER_2: Health is managed by the ECS/ALB target-group health check, not Docker HEALTHCHECK.
# checkov:skip=CKV_DOCKER_3: Demo control-plane container running in an isolated ECS Fargate task; hardening (non-root USER) is left to the adopter.

COPY requirements.txt /opt/requirements.txt
RUN pip install --no-cache-dir -r /opt/requirements.txt

COPY app/ /app/app/
WORKDIR /app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
