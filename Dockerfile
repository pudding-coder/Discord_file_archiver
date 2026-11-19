FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY archivebot ./archivebot
# 자료·DB는 /app/data에 쌓인다. 배포 플랫폼의 볼륨(Railway Volume 등)을 이 경로에 연결하거나,
# docker run 때 -v 로 연결한다. (Railway는 Dockerfile 안의 볼륨 선언을 허용하지 않는다)
CMD ["python", "-m", "archivebot"]
