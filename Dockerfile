FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 WATCH_DATA=/data COOKIE_SECURE=0
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 10001 --home /data --shell /usr/sbin/nologin vpswatch \
 && install -d -m 700 -o vpswatch -g vpswatch /data
COPY *.py ./
COPY merchants ./merchants
COPY static ./static
USER vpswatch
VOLUME /data
EXPOSE 18080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:18080/healthz',timeout=4)" || exit 1
CMD ["gunicorn","--workers","1","--threads","4","--bind","0.0.0.0:18080","--timeout","60","app:app"]
