import os
import time
import threading
import logging
import re
import requests
import sqlite3
from urllib.parse import quote_plus
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from bs4 import BeautifulSoup
from flask import Flask, request, jsonify, send_from_directory, render_template_string
from datetime import datetime
from dotenv import load_dotenv
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

BASE_URL = "https://reparacionespaez.sistemasici.es"
LOGIN_URL = f"{BASE_URL}/"
PRINCIPAL_URL = f"{BASE_URL}/operarios2/principalOperarios.php"

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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                chat_id TEXT PRIMARY KEY
            )
        """)
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

tg_session = requests.Session()

def tg_send(chat, text, markup=None):
    payload = {"chat_id": chat, "text": text, "parse_mode": "HTML"}
    if markup:
        payload["reply_markup"] = markup
    try:
        res = tg_session.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
        data = res.json()
        if data.get("ok"):
            return data["result"]["message_id"]
    except Exception as e:
        logger.error(f"Error tg_send: {e}")
    return None

def tg_edit(chat, msg_id, text, markup=None):
    payload = {"chat_id": chat, "message_id": msg_id, "text": text, "parse_mode": "HTML"}
    if markup:
        payload["reply_markup"] = markup
    try:
        tg_session.post(f"{TELEGRAM_API}/editMessageText", json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Error tg_edit: {e}")

def tg_answer(callback_id):
    try:
        tg_session.post(f"{TELEGRAM_API}/answerCallbackQuery", json={"callback_query_id": callback_id}, timeout=5)
    except Exception as e:
        logger.error(f"Error tg_answer: {e}")

# =========================================================
# BOTONES DEL BOT (MENÚS Y SUBMENÚS)
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
    return {
        "inline_keyboard": [
            [{"text": "⬅️ Volver al Menú", "callback_data": "BACK_MENU"}]
        ]
    }

def botones_usuarios():
    return {
        "inline_keyboard": [
            [{"text": "➕ Agregar", "callback_data": "ADD_USER"}, {"text": "🗑 Eliminar", "callback_data": "DEL_USER"}],
            [{"text": "📋 Listar", "callback_data": "LIST_USERS"}],
            [{"text": "⬅️ Volver", "callback_data": "BACK_MENU"}]
        ]
    }

# =========================================================
# CLASE SICI SCRAPER & MANAGER (CON TOKEN DINÁMICO)
# =========================================================

class SiciManager:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9"
        })
        retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504], raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("https://", adapter)
        self.current_principal_url = PRINCIPAL_URL

    def login(self):
        try:
            self.session.get(LOGIN_URL, timeout=10)
            payload = {
                "usuario": USUARIO,
                "contrasenya": PASSWORD,
                "ENTRAR": "ENTRAR"
            }
            r = self.session.post(LOGIN_URL, data=payload, timeout=15, allow_redirects=True)
            
            if "principaloperarios.php" in r.url.lower() and "login" not in r.url.lower():
                self.current_principal_url = r.url
                logger.info(f"Login en SICI exitoso. URL con token capturada: {r.url}")
                return True
            
            logger.warning(f"El login no redirigió correctamente. URL actual: {r.url}")
            return False
        except Exception as e:
            logger.error(f"Excepción en login SICI: {e}")
            return False

    def obtener_datos_panel(self):
        contadores = {"citas": "0", "caducados": "0", "pendiente": "0", "mensajes": "0"}
        try:
            r = self.session.get(self.current_principal_url, timeout=15)
            if "login" in r.url.lower() or "ENTRAR" in r.text:
                if not self.login():
                    return contadores
                r = self.session.get(self.current_principal_url, timeout=15)
            
            soup = BeautifulSoup(r.text, "html.parser")
            buttons = soup.find_all("button")
            
            for btn in buttons:
                texto_btn = btn.get_text(separator=" ", strip=True).upper()
                match = re.search(r'\[\s*([\d\s\|]+)\s*\]', texto_btn)
                if match:
                    num = match.group(1).strip()
                    if "CITAS" in texto_btn and "CADUCADOS" not in texto_btn and "PENDIENTE" not in texto_btn:
                        contadores["citas"] = num
                    elif "CADUCADOS" in texto_btn:
                        contadores["caducados"] = num
                    elif "PENDIENTE" in texto_btn:
                        contadores["pendiente"] = num
                    elif "MENSAJES" in texto_btn:
                        contadores["mensajes"] = num.replace(" ", "")

            logger.info(f"Contadores leídos correctamente: {contadores}")
            return contadores
        except Exception as e:
            logger.error(f"Error extrayendo contadores: {e}")
            return contadores

sici = SiciManager()

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
            contadores = sici.obtener_datos_panel()
            tg_send(chat, "🤖 <b>Panel S.I.C.I. Operarios</b>\nSelecciona una opción de gestión:", botones_menu(contadores))
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

        if action == "LOGIN":
            ok = sici.login()
            contadores = sici.obtener_datos_panel() if ok else None
            tg_edit(chat, msg_id, "✅ Sesión iniciada y sincronizada con SICI." if ok else "❌ Error en el Login", botones_menu(contadores))

        elif action == "REFRESH":
            contadores = sici.obtener_datos_panel()
            tg_edit(chat, msg_id, "🔄 <b>Panel Actualizado Correctamente</b>", botones_menu(contadores))

        elif action == "CITAS":
            tg_edit(chat, msg_id, "📅 <b>Gestión de Citas</b>\nNo hay citas pendientes actualmente.", botones_volver())

        elif action == "CADUCADOS":
            tg_edit(chat, msg_id, "⚠️ <b>Sección Caducados</b>\nSelecciona una categoría:", botones_caducados_submenu())

        elif action == "CAD_PDT_CITA":
            tg_edit(chat, msg_id, "⚠️ <b>Caducados Pdt. Cita</b>\nListado de elementos pendientes de cita.", botones_volver())

        elif action == "CITAS_CAD":
            tg_edit(chat, msg_id, "🚨 <b>Citas Caducadas</b>\nListado de citas caducadas.", botones_volver())

        elif action == "PENDIENTE_CITA":
            tg_edit(chat, msg_id, "⏳ <b>Pendiente Cita</b>\nCargando listado de partes pendientes...", botones_volver())

        elif action == "MENSAJES":
            tg_edit(chat, msg_id, "✉️ <b>Mensajes y Comunicaciones</b>\nBandeja de mensajes activa.", botones_volver())

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
            contadores = sici.obtener_datos_panel()
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
        return ("Acceso denegado", 401, {"WWW-Authenticate": 'Basic realm="Panel SICI"' })

    archivos = os.listdir(DATA_DIR)
    html = """
    <!doctype html>
    <html>
    <head><title>Panel S.I.C.I.</title>
    <style>body{font-family:Arial;margin:40px;} button{padding:8px;} a{margin:5px;}</style>
    </head>
    <body>
    <h1>☁️ Panel Nube S.I.C.I.</h1>
    <h3>/data</h3>
    <form action="/subir" method="post" enctype="multipart/form-data">
        <input type="file" name="archivo">
        <button>📥 Subir</button>
    </form>
    <hr>
    {% for archivo in archivos %}
    <p>
    📄 <b>{{archivo}}</b>
    <a href="/descargar/{{archivo}}">⬇ Descargar</a>
    <a href="/eliminar/{{archivo}}" onclick="return confirm('¿Eliminar?')">🗑 Eliminar</a>
    </p>
    {% endfor %}
    </body>
    </html>
    """
    return render_template_string(html, archivos=archivos)

@app.route("/subir", methods=["POST"])
def subir_archivo():
    if not comprobar_login():
        return "No autorizado", 401
    archivo = request.files.get("archivo")
    if archivo and archivo.filename:
        filename = secure_filename(archivo.filename)
        archivo.save(os.path.join(DATA_DIR, filename))
    return 'Archivo subido<br><a href="/">Volver</a>'

@app.route("/descargar/<nombre>")
def descargar_archivo(nombre):
    if not comprobar_login():
        return "No autorizado", 401
    return send_from_directory(DATA_DIR, secure_filename(nombre), as_attachment=True)

@app.route("/eliminar/<nombre>")
def eliminar_archivo(nombre):
    if not comprobar_login():
        return "No autorizado", 401
    filename = secure_filename(nombre)
    if "db" in filename:
        return '❌ No se puede eliminar la base de datos.<br><a href="/">Volver</a>'
    ruta = os.path.join(DATA_DIR, filename)
    if os.path.exists(ruta):
        os.remove(ruta)
    return '✅ Eliminado<br><a href="/">Volver</a>'

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
