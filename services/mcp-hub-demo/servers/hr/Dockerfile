FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY common ./common
COPY hr ./hr
ENV PORT=9002
EXPOSE 9002
CMD ["python", "-m", "hr.server"]
