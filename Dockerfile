FROM python:3.12-slim

WORKDIR /app

COPY index.html server.py ./

ENV PORT=8899 \
    PYTHONUNBUFFERED=1

EXPOSE 8899

# 公网部署建议：docker run -e ACCESS_TOKEN=你的口令 ...
CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "8899", "--no-open"]
