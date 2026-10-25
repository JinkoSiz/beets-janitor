# -*- coding: utf-8 -*-
"""Вход в пульт: пароль хозяина и пароли гостей из окружения.

Пульт смотрит в интернет через Nginx Proxy Manager, поэтому без входа не
открывается ничего, кроме страницы входа, статики и проверки здоровья.
Пароль хозяина не задан — не впускаем никого: безопаснее, чем открытая дверь.
Перебор режется: после пяти неудач с одного адреса — пауза на 10 минут.

Гости (PANEL_GUESTS=имя:пароль,имя2:пароль2; пароль можно и хэшем Django)
видят только дискографию и свои заявки: разобрать исполнителя и скачать, чего
нет. Решения, карантин, журнал и настройки им закрыты. Убрал гостя из
окружения — его сессия перестаёт пускать со следующего запроса.
"""
import hmac
import os
import time
from urllib.parse import quote

from django.contrib.auth.hashers import check_password
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme

OPEN = ("/login", "/static/", "/health")
# куда пускают гостя; разбор дорожек «уже есть / другая версия» — решение хозяина
GUEST_OPEN = ("/discography/", "/artists/search", "/audio/deezer/", "/requests/", "/logout")
GUEST_CLOSED = ("/discography/track/",)
GUEST_HOME = "/discography/"
FAILS_MAX = 5
FAILS_WINDOW = 600
_fails = {}


def configured():
    return bool(os.environ.get("PANEL_PASSWORD_HASH") or os.environ.get("PANEL_PASSWORD"))


def secret_ok(p, secret):
    """Пароль против значения из окружения: хэш Django или открытый текст."""
    if not p or not secret:
        return False
    if secret.split("$", 1)[0] in ("pbkdf2_sha256", "pbkdf2_sha1", "argon2", "bcrypt_sha256", "scrypt"):
        return check_password(p, secret)
    return hmac.compare_digest(p.encode("utf-8"), secret.encode("utf-8"))


def password_ok(p):
    h = os.environ.get("PANEL_PASSWORD_HASH", "")
    if h:
        return check_password(p, h)
    return secret_ok(p, os.environ.get("PANEL_PASSWORD", ""))


def guests():
    out = {}
    for part in os.environ.get("PANEL_GUESTS", "").split(","):
        name, _, secret = part.strip().partition(":")
        if name.strip() and secret.strip():
            out[name.strip()] = secret.strip()
    return out


def guest_for(p):
    """Имя гостя с этим паролем или None."""
    for name, secret in guests().items():
        if secret_ok(p, secret):
            return name
    return None


def guest_of(request):
    """Имя гостя этой сессии; у хозяина — None."""
    return request.session.get("guest")


def _away(request, to):
    if request.headers.get("HX-Request"):
        # htmx не проходит по обычному редиректу — просим его сменить страницу
        resp = HttpResponse(status=204)
        resp["HX-Redirect"] = to
        return resp
    return redirect(to)


def client_ip(request):
    ip = request.META.get("HTTP_X_REAL_IP") or request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0]
    return (ip or request.META.get("REMOTE_ADDR", "")).strip()


def blocked(ip):
    now = time.time()
    hits = [t for t in _fails.get(ip, []) if now - t < FAILS_WINDOW]
    _fails[ip] = hits
    return len(hits) >= FAILS_MAX


class LoginRequired:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(OPEN):
            return self.get_response(request)
        if not request.session.get("ok"):
            if request.headers.get("HX-Request"):
                return _away(request, "/login")
            return redirect("/login?next=" + quote(request.get_full_path()))
        guest = guest_of(request)
        if guest is not None:
            if guest not in guests():
                request.session.flush()
                return _away(request, "/login")
            if not request.path.startswith(GUEST_OPEN) or request.path.startswith(GUEST_CLOSED):
                return _away(request, GUEST_HOME)
        return self.get_response(request)


def login_view(request):
    error = None
    nxt = request.GET.get("next") or request.POST.get("next") or "/"
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        nxt = "/"
    if not configured():
        error = "Пароль не задан: укажите PANEL_PASSWORD в окружении контейнера."
    elif request.method == "POST":
        ip = client_ip(request)
        p = request.POST.get("password", "")
        guest = None if blocked(ip) or password_ok(p) else guest_for(p)
        if blocked(ip):
            error = "Слишком много попыток. Подождите десять минут."
        elif password_ok(p):
            _fails.pop(ip, None)
            request.session.cycle_key()
            request.session["ok"] = True
            request.session.pop("guest", None)
            return redirect(nxt)
        elif guest:
            _fails.pop(ip, None)
            request.session.cycle_key()
            request.session["ok"] = True
            request.session["guest"] = guest
            return redirect(nxt if nxt.startswith(GUEST_OPEN) else GUEST_HOME)
        else:
            _fails.setdefault(ip, []).append(time.time())
            error = "Пароль не подошёл."
    return render(request, "pult/login.html", {"error": error, "next": nxt})


def logout_view(request):
    if request.method == "POST":
        request.session.flush()
    return redirect("/login")


def health(request):
    return HttpResponse("ok", content_type="text/plain")
