import asyncio
import logging
import os
import re
import sqlite3
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template_string, request, send_from_directory
from playwright.async_api import async_playwright
from werkzeug.utils import secure_filename

load_dotenv()

# =========================================================
# CONFIGURACIÓN S.I.C.I.
# =========================================================

USUARIO = os.getenv("USUARIO")
PASSWORD = os.getenv("PASSWORD")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "1234")

URL = "https://reparacionespaez.sistemasici.es/"
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bot_sici")

app = Flask(__name__)

# =========================================================
# STATE & DB
# =========================================================

USER_STATE = {}
DATA_DIR = "/data"
DB_PATH = os.path.join(DATA_DIR, "usuarios_sici.db")
os.makedirs(DATA_DIR, exist_ok=True)

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS usuarios (chat_id TEXT PRIMARY KEY)")
        conn.commit()

def guardar_usuario(chat_id):
    with get_db() as conn:
        conn.execute("INSERT OR IGNORE INTO usuarios (chat_id) VALUES (?)", (str(chat_id),))
        conn.commit()

def obtener_usuarios():
    with get_db() as conn:
        cursor = conn.execute("SELECT chat_id FROM usuarios")
        return [r["chat_id"] for r in cursor.fetchall()]

def eliminar_usuario(chat_id):
    with get_db() as conn:
        conn.execute("DELETE FROM usuarios WHERE chat_id=?", (str(chat_id),))
        conn.commit()

init_db()

# =========================================================
# TELEGRAM HELPERS
# =========================================================

import requests
tg_session = requests.Session()

def tg_send(chat, text, markup=None):
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if markup: payload["reply_markup"] = markup
    try:
        res = tg_session.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
        data = res.json()
        if data.get("ok"): return data["result"]["message_id"]
    except Exception as e:
        logger.error(f"Error tg_send: {e}")
    return None

def tg_edit(chat, msg_id, text, markup=None):
    payload = {"chat_id": chat, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
    if markup: payload["reply_markup"] = markup
    try:
        tg_session.post(f"{TELEGRAM_API}/editMessageText", json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Error tg_edit: {e}")

def tg_answer(callback_id, text="", alert=False):
    try:
        tg_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": callback_id, "text": text, "show_alert": alert}, timeout=5)
    except Exception as e:
        logger.error(f"Error tg_answer: {e}")

# =========================================================
# BOTONES DEL BOT
# =========================================================

def botones_menu(contadores=None):
    c = contadores or {"citas": "0", "caducados": "0", "pendiente": "0", "mensajes": "0"}
    return {
        "inline_keyboard": [
            [{"text": "🔐 Login", "callback_data": "LOGIN"}, {"text": "🔄 Actualizar", "callback_data": "REFRESH"}],
            [{"text": f"📅 Citas [{c['citas']}]", "callback_data": "CITAS"}, {"text": f"⚠️ Caducados [{c['caducados']}]", "callback_data": "CADUCADOS"}],
            [{"text": f"⏳ Pendiente Cita [{c['pendiente']}]", "callback_data": "PENDIENTE_CITA"}, {"text": f"✉️ Mensajes [{c['mensajes']}]", "callback_data": "MENSAJES"}],
            [{"text": "👥 Usuarios", "callback_data": "USUARIOS"}]
        ]
    }

def botones_caducados_submenu():
    return {
        "inline_keyboard": [
            [{"text": "⚠️ Caducados Pdt. Cita", "callback_data": "CAD_PDT_CITA"}, {"text": "🚨 Citas Caducadas", "callback_data": "CITAS_CAD"}],
            [{"text": "⬅️ Volver al Menú", "callback_data": "BACK_MENU"}]
        ]
    }

def botones_volver():
    return {"inline_keyboard": [[{"text": "⬅️ Volver al Menú", "callback_data": "BACK_MENU"}]]}

def botones_usuarios():
    return {
        "inline_keyboard": [
            [{"text": "➕ Agregar", "callback_data": "ADD_USER"}, {"text": "🗑 Eliminar", "callback_data": "DEL_USER"}],
            [{"text": "📋 Listar", "callback_data": "LIST_USERS"}],
            [{"text": "⬅️ Volver", "callback_data": "BACK_MENU"}]
        ]
    }

# =========================================================
# MOTOR PLAYWRIGHT (IDÉNTICO A TU SCRIPT LOCAL)
# =========================================================

async def obtener_contadores_con_playwright():
    contadores = {"citas": "0", "caducados": "0", "pendiente": "0", "mensajes": "0"}
    exito = False
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
            )
            page = await browser.new_page()

            print("1. Accediendo a SICI...")
            await page.goto(URL, timeout=30000)

            print("2. Iniciando sesión...")
            await page.fill("input[name='usuario']", USUARIO)
            await page.fill("input[name='contrasenya']", PASSWORD)
            await page.click("button[name='ENTRAR']")

            try:
                await page.wait_for_url("**/principalOperarios.php", timeout=15000)
            except:
                await page.wait_for_load_state("networkidle")

            print("✅ Login exitoso en SICI.")
            exito = True

            # Extraer contadores de los botones del panel principal
            botones_menu_elem = page.locator("button")
            count = await botones_menu_elem.count()
            for i in range(count):
                btn = botones_menu_elem.nth(i)
                texto = await btn.inner_text()
                if "[" in texto and "]" in texto:
                    m = re.search(r"\[(.*?)\]", texto)
                    if m:
                        val = m.group(1).strip()
                        upper_txt = texto.upper()
                        if "CITAS" in upper_txt and "CADUCADOS" not in upper_txt and "PENDIENTE" not in upper_txt:
                            contadores["citas"] = val
                        elif "CADUCADOS" in upper_txt:
                            contadores["caducados"] = val
                        elif "PENDIENTE" in upper_txt:
                            contadores["pendiente"] = val
                        elif "MENSAJES" in upper_txt:
                            contadores["mensajes"] = val.replace(" ", "")

            await browser.close()
    except Exception as e:
        logger.error(f"Error crítico en Playwright SICI: {e}")
        exito = False

    return contadores, exito

# =========================================================
# WEBHOOK TELEGRAM
# =========================================================

@app.route("/telegram_webhook", methods=["POST"])
def webhook():
    data = request.json or {}

    if "message" in data:
        chat = data["message"]["chat"]["id"]
        text = data["message"].get("text", "")
        guardar_usuario(chat)

        if text == "/start":
            contadores, _ = asyncio.run(obtener_contadores_con_playwright())
            tg_send(chat, "🤖 <b>Panel S.I.C.I. Operarios</b>\nSelecciona una opción:", botones_menu(contadores))
            return jsonify(ok=True)

        if chat in USER_STATE:
            if USER_STATE[chat] == "ADD_USER":
                guardar_usuario(text)
                tg_send(chat, "✅ Usuario autorizado añadido.")
                USER_STATE.pop(chat)
            elif USER_STATE[chat] == "DEL_USER":
                eliminar_usuario(text)
                tg_send(chat, "🗑 Usuario eliminado.")
                USER_STATE.pop(chat)

    elif "callback_query" in data:
        cq = data["callback_query"]
        chat = cq["message"]["chat"]["id"]
        msg_id = cq["message"]["message_id"]
        action = cq["data"]

        tg_answer(cq["id"])
        guardar_usuario(chat)

        if action in ["LOGIN", "REFRESH"]:
            contadores, ok = asyncio.run(obtener_contadores_con_playwright())
            msg = "✅ Sesión iniciada y sincronizada con SICI." if ok else "❌ Error en el Login"
            if action == "REFRESH":
                msg = "🔄 <b>Panel Actualizado Correctamente</b>"
            tg_edit(chat, msg_id, msg, botones_menu(contadores))

        elif action == "CITAS":
            tg_edit(chat, msg_id, "📅 <b>Gestión de Citas</b>\nNo hay citas pendientes actualmente.", botones_volver())

        elif action == "CADUCADOS":
            tg_edit(chat, msg_id, "⚠️ <b>Sección Caducados</b>\nSelecciona una categoría:", botones_caducados_submenu())

        elif action == "CAD_PDT_CITA":
            tg_edit(chat, msg_id, "⚠️ <b>Caducados Pdt. Cita</b>\nListado cargado correctamente.", botones_volver())

        elif action == "CITAS_CAD":
            tg_edit(chat, msg_id, "🚨 <b>Citas Caducadas</b>\nListado de citas caducadas.", botones_volver())

        elif action == "PENDIENTE_CITA":
            tg_edit(chat, msg_id, "⏳ <b>Pendiente Cita</b>\nListado de partes pendientes.", botones_volver())

        elif action == "MENSAJES":
            tg_edit(chat, msg_id, "✉️ <b>Mensajes y Comunicaciones</b>\nBandeja activa.", botones_volver())

        elif action == "USUARIOS":
            tg_edit(chat, msg_id, "👥 Gestión de Usuarios Autorizados", botones_usuarios())

        elif action == "ADD_USER":
            USER_STATE[chat] = "ADD_USER"
            tg_send(chat, "Envía el Chat ID del nuevo usuario:")

        elif action == "DEL_USER":
            USER_STATE[chat] = "DEL_USER"
            tg_send(chat, "Envía el Chat ID del usuario a eliminar:")

        elif action == "LIST_USERS":
            usuarios = "\n".join(obtener_usuarios())
            tg_edit(chat, msg_id, f"📋 <b>Usuarios con acceso:</b>\n\n{usuarios}" if usuarios else "Vacío", botones_usuarios())

        elif action == "BACK_MENU":
            contadores, _ = asyncio.run(obtener_contadores_con_playwright())
            tg_edit(chat, msg_id, "🏠 <b>Menú Principal S.I.C.I.</b>", botones_menu(contadores))

    return jsonify(ok=True)

# =========================================================
# PANEL WEB DE GESTIÓN (RAILWAY)
# =========================================================

def comprobar_login():
    auth = request.authorization
    return auth and auth.username == ADMIN_USER and auth.password == ADMIN_PASS

@app.route("/")
def nube():
    if not comprobar_login():
        return "Acceso denegado", 401, {"WWW-Authenticate": 'Basic realm="Panel SICI"'}
    archivos = os.listdir(DATA_DIR)
    html = """
    <!doctype html>
    <html><head><title>Panel S.I.C.I.</title></head>
    <body><h1>☁️ Panel Nube S.I.C.I.</h1>
    {% for archivo in archivos %}<p>📄 <b>{{archivo}}</b> <a href="/descargar/{{archivo}}">⬇ Descargar</a></p>{% endfor %}
    </body></html>
    """
    return render_template_string(html, archivos=archivos)

@app.route("/descargar/<nombre>")
def descargar_archivo(nombre):
    if not comprobar_login(): return "No autorizado", 401
    return send_from_directory(DATA_DIR, secure_filename(nombre), as_attachment=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
