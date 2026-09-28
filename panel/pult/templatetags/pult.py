# -*- coding: utf-8 -*-
"""Фильтры шаблонов пульта: даты по-русски, проценты, размеры, аватарки."""
import datetime
import hashlib

from django import template

register = template.Library()

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]
COLORS = ["#7A2E4E", "#20394F", "#4E4A1F", "#6B3A12", "#8A2A1E", "#2F2846", "#1F4E5F", "#3F2A2A", "#3B4A2A", "#5A2F5E"]
NBSP = " "


def _dt(v):
    if isinstance(v, datetime.datetime):
        return v
    if isinstance(v, datetime.date):
        return datetime.datetime(v.year, v.month, v.day)
    try:
        return datetime.datetime.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


@register.filter
def num(v):
    try:
        return "{:,}".format(int(v)).replace(",", NBSP)
    except (TypeError, ValueError):
        return v


@register.filter
def dur(v):
    try:
        s = int(round(float(v)))
    except (TypeError, ValueError):
        return "—"
    return "%d:%02d" % (s // 60, s % 60) if s < 3600 else "%d:%02d:%02d" % (s // 3600, s % 3600 // 60, s % 60)


@register.filter
def pct(v):
    try:
        return "%d%%" % round(float(v) * 100)
    except (TypeError, ValueError):
        return "—"


@register.filter
def pct100(v):
    try:
        return round(float(v) * 100)
    except (TypeError, ValueError):
        return 0


@register.filter
def pctcls(v):
    """Цвет плашки сходства: зелёная — та же запись, красная — подмена."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    return "ok" if v >= 0.85 else ("warn" if v >= 0.60 else "bad")


@register.filter
def size(v):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if v < 1024 or unit == "ТБ":
            s = ("%.1f" % v if unit in ("ГБ", "ТБ") and v < 100 else "%d" % round(v)).replace(".", ",")
            return "%s%s%s" % (s, NBSP, unit)
        v /= 1024.0


@register.filter
def ddmm(v):
    d = _dt(v)
    return d.strftime("%d.%m") if d else "—"


@register.filter
def ddmm_hm(v):
    d = _dt(v)
    return d.strftime("%d.%m %H:%M") if d else "—"


@register.filter
def hm(v):
    d = _dt(v)
    return d.strftime("%H:%M") if d else "—"


@register.filter
def rudate(v):
    d = _dt(v)
    if not d:
        return str(v or "—")
    return "%d %s %d" % (d.day, MONTHS[d.month - 1], d.year)


@register.filter
def when(v):
    """«сегодня 05:22», «вчера 23:10» или «22.09 16:44»."""
    d = _dt(v)
    if not d:
        return "—"
    today = datetime.date.today()
    if d.date() == today:
        return "сегодня " + d.strftime("%H:%M")
    if d.date() == today - datetime.timedelta(days=1):
        return "вчера " + d.strftime("%H:%M")
    return d.strftime("%d.%m %H:%M")


@register.filter
def plural(n, forms):
    """{{ n|plural:"трек,трека,треков" }}"""
    one, few, many = forms.split(",")
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return many
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


@register.filter
def initials(name):
    words = [w for w in str(name or "?").replace("&", " ").split() if w]
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    w = words[0] if words else "?"
    return (w[:1].upper() + w[1:2].lower())


@register.filter
def color(name):
    h = int(hashlib.md5(str(name or "").encode("utf-8")).hexdigest()[:6], 16)
    return COLORS[h % len(COLORS)]


@register.filter
def short(p):
    """Путь без корня фонотеки, карантина или incoming."""
    from pult import data
    return data.short(p)


@register.filter
def get(d, key):
    try:
        return d.get(key)
    except AttributeError:
        return None


@register.filter
def mins(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return ""
    return "%d мин" % max(1, round(v / 60)) if v >= 60 else "%d с" % v


@register.filter
def secs_mmss(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return "—"
    return "%d:%02d" % (v // 60, v % 60)
