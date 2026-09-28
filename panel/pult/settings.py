# -*- coding: utf-8 -*-
"""Настройки пульта.

ORM Django здесь не используется: всё, что пульт знает, лежит в двух SQLite
— общей базе набора (janitor.db, читаем и пишем) и базе beets (library.db,
только читаем). Сессии — в подписанной куке, отдельная база под них не
нужна.

Окружение:
  PANEL_PASSWORD        пароль входа (или PANEL_PASSWORD_HASH — хэш Django)
  PANEL_SECRET_KEY      ключ подписи; если не задан — создаётся и хранится
                        в JANITOR_DIR/panel-secret
  PANEL_HOSTS           допустимые имена хоста через запятую (по умолчанию *)
  PANEL_ORIGINS         https://имя-пульта — для проверки CSRF за прокси
  PANEL_SECURE_COOKIES  0 — разрешить вход по голому http (проверка в LAN)
  SCRIPTS_DIR           где лежат скрипты beets-janitor
  плюс пути набора (MUSIC_DIR, CONFIG_DIR, JANITOR_DIR…), см. scripts/env.py
"""
import os
import secrets
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.environ.get("SCRIPTS_DIR") or os.path.join(os.path.dirname(os.path.dirname(BASE_DIR)), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import env as janitor_env  # noqa: E402


def _secret():
    v = os.environ.get("PANEL_SECRET_KEY")
    if v:
        return v
    if os.environ.get("DJANGO_COLLECTING"):
        return "collectstatic"
    path = os.path.join(janitor_env.JANITOR_DIR, "panel-secret")
    try:
        with open(path) as f:
            v = f.read().strip()
        if v:
            return v
    except OSError:
        pass
    v = secrets.token_urlsafe(50)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(v)
    os.chmod(path, 0o600)
    return v


SECRET_KEY = _secret()
DEBUG = os.environ.get("PANEL_DEBUG") == "1"
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("PANEL_HOSTS", "*").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("PANEL_ORIGINS", "").split(",") if o.strip()]
# за Nginx Proxy Manager: схему и хост берём из заголовков прокси
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True

INSTALLED_APPS = ["django.contrib.staticfiles", "pult"]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "pult.auth.LoginRequired",
]
ROOT_URLCONF = "pult.urls"
WSGI_APPLICATION = "pult.wsgi.application"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.template.context_processors.csrf",
        "pult.views.nav",
    ]},
}]
DATABASES = {}

SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
SESSION_COOKIE_AGE = 30 * 86400
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_NAME = "pult"
_secure = os.environ.get("PANEL_SECURE_COOKIES", "1") != "0"
SESSION_COOKIE_SECURE = _secure
CSRF_COOKIE_SECURE = _secure
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

LANGUAGE_CODE = "ru"
USE_I18N = False
USE_TZ = False
TIME_ZONE = os.environ.get("TZ", "Europe/Moscow")

STATIC_URL = "/static/"
STATIC_ROOT = os.path.join(os.path.dirname(BASE_DIR), "static")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
FILE_UPLOAD_TEMP_DIR = None

# картинка обложки или звуковой файл на замену подмены
DATA_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 5 * 1024 * 1024
UPLOAD_LIMIT = 200 * 1024 * 1024

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}
