# -*- coding: utf-8 -*-
"""Вход в пульт: один пароль из окружения.

Пульт смотрит в интернет через Nginx Proxy Manager, поэтому без входа не
открывается ничего, кроме страницы входа, статики и проверки здоровья.
Пароль не задан — не впускаем никого: безопаснее, чем открытая дверь.
Перебор режется: после пяти неудач с одного адреса — пауза на 10 минут.
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
FAILS_MAX = 5
FAILS_WINDOW = 600
_fails = {}


def configured():
    return bool(os.environ.get("PANEL_PASSWORD_HASH") or os.environ.get("PANEL_PASSWORD"))


def password_ok(p):
    h = os.environ.get("PANEL_PASSWORD_HASH", "")
    if h:
        return check_password(p, h)
    plain = os.environ.get("PANEL_PASSWORD", "")
    return bool(plain) and hmac.compare_digest(p.encode("utf-8"), plain.encode("utf-8"))


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
        if request.path.startswith(OPEN) or request.session.get("ok"):
            return self.get_response(request)
        if request.headers.get("HX-Request"):
            # htmx не проходит по обычному редиректу — просим его сменить страницу
            resp = HttpResponse(status=204)
            resp["HX-Redirect"] = "/login"
            return resp
        return redirect("/login?next=" + quote(request.get_full_path()))


def login_view(request):
    error = None
    nxt = request.GET.get("next") or request.POST.get("next") or "/"
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        nxt = "/"
    if not configured():
        error = "Пароль не задан: укажите PANEL_PASSWORD в окружении контейнера."
    elif request.method == "POST":
        ip = client_ip(request)
        if blocked(ip):
            error = "Слишком много попыток. Подождите десять минут."
        elif password_ok(request.POST.get("password", "")):
            _fails.pop(ip, None)
            request.session.cycle_key()
            request.session["ok"] = True
            return redirect(nxt)
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
