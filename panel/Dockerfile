# Пульт beets-janitor. Собирается из корня репозитория:
#   docker build -f panel/Dockerfile -t beets-janitor-panel .
FROM python:3.12-slim

# fpcalc нужен разбору дискографии: превью Deezer сверяется с файлом по отпечатку
RUN apt-get update \
 && apt-get install -y --no-install-recommends libchromaprint-tools curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY panel/requirements.txt panel/requirements.txt
RUN pip install --no-cache-dir -r panel/requirements.txt

COPY scripts scripts
COPY panel panel

# htmx 2.0.4 лежит в репозитории (panel/pult/static/pult/htmx.min.js): сборка
# не зависит от CDN — с домашнего сервера unpkg отвечает через раз
RUN cd panel && DJANGO_COLLECTING=1 python manage.py collectstatic --noinput -v 0

ENV SCRIPTS_DIR=/app/scripts PYTHONUNBUFFERED=1
WORKDIR /app/panel
EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s CMD curl -fs http://127.0.0.1:8000/health || exit 1
# один процесс: фоновые задачи (разбор дискографии) живут в его потоках
CMD ["gunicorn", "pult.wsgi", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "180", "--access-logfile", "-"]
