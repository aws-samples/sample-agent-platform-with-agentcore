FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common ./common
COPY order ./order
ENV PORT=9001
EXPOSE 9001
CMD ["python", "-m", "order.server"]
