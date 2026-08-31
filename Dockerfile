FROM python:3.12-slim

WORKDIR /app

COPY index.html server.py ./

ENV PORT=8899 \
    PYTHONUNBUFFERED=1

EXPOSE 8899

# 换端口：docker run -e PORT=9000 -p 9000:9000 ...（也可只改映射 -p 9000:8899）
CMD ["python", "server.py", "--host", "0.0.0.0", "--no-open"]
