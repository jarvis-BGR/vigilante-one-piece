#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vigilante One Piece — Fase 1
Lee la ficha de un producto en una tienda, detecta cambios de estado
y avisa por Telegram solo cuando hay algo relevante.
No compra ni reserva nada: solo informa.
"""
import json, os, re, html, datetime
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
PRODUCTOS = os.path.join(BASE, "productos.json")
ESTADO = os.path.join(BASE, "estado.json")

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")

MAPEO = {
    "in stock": ("DISPONIBLE", "🟢"),
    "instock": ("DISPONIBLE", "🟢"),
    "available for order": ("PREVENTA", "🟡"),
    "preorder": ("PREVENTA", "🟡"),
    "backorder": ("PREVENTA", "🟡"),
    "out of stock": ("AGOTADO", "🔴"),
    "discontinued": ("AGOTADO", "🔴"),
}
FECHA_RE = re.compile(r'(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})')


def cargar_json(ruta, por_defecto):
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return por_defecto


def guardar_json(ruta, datos):
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)


def leer_metas(contenido):
    metas = {}
    for m in re.finditer(r"<meta\b[^>]*>", contenido, re.I):
        tag = m.group(0)
        clave = re.search(r'(?:property|name)\s*=\s*(["\'])(.*?)\1', tag, re.I)
        valor = re.search(r'content\s*=\s*(["\'])(.*?)\1', tag, re.I)
        if clave and valor:
            metas[clave.group(2).lower()] = html.unescape(valor.group(2))
    return metas


def normaliza_estado(disponibilidad):
    return MAPEO.get((disponibilidad or "").strip().lower(), ("DESCONOCIDO", "⚪"))


def leer_tienda(url):
    r = requests.get(url, headers={"User-Agent": UA,
                                   "Accept-Language": "es-ES,es;q=0.9"}, timeout=30)
    r.raise_for_status()
    contenido = r.text
    metas = leer_metas(contenido)
    disp = metas.get("product:availability", "")
    estado, emoji = normaliza_estado(disp)
    texto = re.sub(r"<[^>]+>", " ", contenido)
    fecha = ""
    for kw in ("preventa", "reserva", "disponible el", "fecha de salida", "salida"):
        idx = texto.lower().find(kw)
        if idx != -1:
            m = FECHA_RE.search(texto, idx, idx + 140)
            if m:
                fecha = m.group(1)
                break
    return {
        "nombre": metas.get("og:title", ""),
        "estado": estado,
        "emoji": emoji,
        "disponibilidad_raw": disp,
        "precio": metas.get("product:price:amount", ""),
        "moneda": metas.get("product:price:currency", ""),
        "fecha": fecha,
    }


def formatea_precio(p, moneda):
    if not p:
        return ""
    try:
        s = f"{float(p):.2f}".replace(".", ",")
    except ValueError:
        s = p
    simbolo = {"EUR": "€", "USD": "$", "GBP": "£"}.get((moneda or "").upper(), moneda or "")
    return f"{s} {simbolo}".strip()


def mensaje(item, info, tipo, anterior=None):
    L = []
    L.append("🆕 <b>Vigilancia iniciada</b>" if tipo == "inicio"
             else f"{info['emoji']} <b>¡Cambio de estado!</b>")
    L.append(f"<b>{info['nombre'] or item.get('nombre','')}</b>")
    L.append(f"🏬 Tienda: {item['tienda']}")
    if tipo == "cambio" and anterior:
        L.append(f"📦 Estado: {anterior} ➜ {info['emoji']} <b>{info['estado']}</b>")
    else:
        L.append(f"📦 Estado: {info['emoji']} <b>{info['estado']}</b>")
    precio = formatea_precio(info["precio"], info["moneda"])
    if precio:
        L.append(f"💶 Precio: {precio}")
    if info["fecha"]:
        L.append(f"📅 Fecha prevista: {info['fecha']}")
    L.append(f'🔗 <a href="{item["url"]}">Ver en la tienda ▸</a>')
    return "\n".join(L)


def envia_telegram(texto):
    if not TOKEN or not CHAT_ID:
        print("[AVISO] Falta TELEGRAM_TOKEN o TELEGRAM_CHAT_ID; no se envía el aviso.")
        return False
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": texto,
                                     "parse_mode": "HTML",
                                     "disable_web_page_preview": "false"}, timeout=30)
        if not (r.ok and r.json().get("ok")):
            print("[ERROR Telegram]", r.status_code, r.text[:300])
            return False
        return True
    except Exception as e:
        print("[ERROR Telegram]", e)
        return False


def main():
    productos = cargar_json(PRODUCTOS, [])
    estado = cargar_json(ESTADO, {})
    estado.setdefault("_items", {})
    cambios = 0
    for item in productos:
        clave = item.get("id") or f'{item["tienda"]}|{item.get("ean", "")}'
        try:
            info = leer_tienda(item["url"])
        except Exception as e:
            print(f"[ERROR leyendo {clave}] {e}")
            continue
        if not info["nombre"] and not info["precio"] and info["estado"] == "DESCONOCIDO":
            print(f"[AVISO] Lectura vacia o bloqueada en {clave}; no se actualiza su estado.")
            continue
        prev = estado["_items"].get(clave)
        print(f"{clave}: {info['estado']} (raw='{info['disponibilidad_raw']}') "
              f"precio={info['precio']}{info['moneda']} fecha='{info['fecha']}'")
        if prev is None:
            if envia_telegram(mensaje(item, info, "inicio")):
                cambios += 1
        elif prev.get("estado") != info["estado"]:
            if envia_telegram(mensaje(item, info, "cambio", anterior=prev.get("estado"))):
                cambios += 1
        estado["_items"][clave] = {
            "estado": info["estado"], "precio": info["precio"],
            "fecha": info["fecha"], "nombre": info["nombre"] or item.get("nombre", ""),
        }
    estado["_ultima_ejecucion"] = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    guardar_json(ESTADO, estado)
    print(f"Hecho. Avisos enviados: {cambios}")


if __name__ == "__main__":
    main()
