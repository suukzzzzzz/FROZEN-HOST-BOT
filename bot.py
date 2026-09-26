#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════
#  24x7 LAZZYXMOD HOSTING BOT
#  Works: Railway, Render, Koyeb, VPS, Termux — everywhere
#  Fix: is_auth column + all DB migration + crash recovery
# ═══════════════════════════════════════════════════════════════

import os, sys, subprocess, threading, time, shutil, zipfile
import tarfile, sqlite3, ast, importlib, html as html_lib
import logging
from datetime import datetime
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer  # <-- Added for Render

# ── AUTO INSTALL ──────────────────────────────────────────────
def auto_install():
    pkgs = {"pyTelegramBotAPI": "telebot", "requests": "requests"}
    for pip_name, import_name in pkgs.items():
        try:
            __import__(import_name)
        except ImportError:
            print(f"📦 Installing {pip_name}...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_name, "-q"])
            print(f"✅ {pip_name} installed")

auto_install()

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

import telebot
from telebot.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton
)

# ── LOGGING ───────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ── CONFIG ────────────────────────────────────────────────────
BOT_TOKEN       = os.environ.get("BOT_TOKEN",       "8950258373:AAEuPIgyvlL_ADQt3FXgfwBEJLSzQYJ_1kk")
ADMIN_ID        = int(os.environ.get("ADMIN_ID",    "7597712290"))
SECRET_PASSWORD = os.environ.get("SECRET_PASSWORD", "primelazzy888")

CPU_THRESHOLD       = float(os.environ.get("CPU_THRESHOLD",       "90.0"))
MEMORY_THRESHOLD    = float(os.environ.get("MEMORY_THRESHOLD",    "90.0"))
MAX_RUNNING         = int(os.environ.get("MAX_RUNNING_PROCESSES", "10"))
MAX_FILES_PER_USER  = int(os.environ.get("MAX_FILES_PER_USER",    "999"))

# ── PATHS ─────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.resolve()
DATA_DIR    = BASE_DIR / "data"
DB_PATH     = DATA_DIR / "metadata.db"
UPLOADS_DIR = DATA_DIR / "uploads"
LOGS_DIR    = DATA_DIR / "logs"
TEMP_DIR    = DATA_DIR / "temp"

for d in [DATA_DIR, UPLOADS_DIR, LOGS_DIR, TEMP_DIR]:
    d.mkdir(parents=True, exist_ok=True)

START_TIME = datetime.utcnow()

# ═══════════════════════════════════════════════════════════════
#  WEB SERVER FOR RENDER (HEALTH CHECK)
# ═══════════════════════════════════════════════════════════════
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain')
        self.end_headers()
        self.wfile.write(b"Bot is running perfectly!")
        
    def log_message(self, format, *args):
        # Logs ko clean rakhne ke liye HTTP requests ko ignore karein
        pass

def run_health_check():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    log.info(f"🌐 Health check server running on port {port}")
    server.serve_forever()

# Health check thread start karein
threading.Thread(target=run_health_check, daemon=True).start()

# ═══════════════════════════════════════════════════════════════
#  DATABASE  — safe migration so old DBs don't crash
# ═══════════════════════════════════════════════════════════════
conn    = sqlite3.connect(str(DB_PATH), check_same_thread=False)
conn.row_factory = sqlite3.Row
db_lock = threading.Lock()

def _col_exists(cur, table, col):
    cur.execute(f"PRAGMA table_info({table})")
    return any(row["name"] == col for row in cur.fetchall())

def init_db():
    with db_lock:
        cur = conn.cursor()

        # ── files table ──────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id     INTEGER,
                username    TEXT,
                filename    TEXT,
                orig_name   TEXT,
                path        TEXT,
                uploaded_at TEXT,
                file_type   TEXT,
                pid         INTEGER,
                status      TEXT DEFAULT 'Stopped'
            )
        """)

        # ── runs table ───────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id     INTEGER,
                started_at  TEXT,
                finished_at TEXT,
                pid         INTEGER,
                log_path    TEXT,
                exit_code   INTEGER
            )
        """)

        # ── users table ──────────────────────────────────────
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id   INTEGER PRIMARY KEY,
                username  TEXT,
                joined_at TEXT,
                last_seen TEXT,
                is_auth   INTEGER DEFAULT 0
            )
        """)

        # ── SAFE MIGRATION: add missing columns ──────────────
        migrations = {
            "files": [
                ("pid",    "INTEGER"),
                ("status", "TEXT DEFAULT 'Stopped'"),
            ],
            "users": [
                ("is_auth",   "INTEGER DEFAULT 0"),
                ("joined_at", "TEXT"),
                ("last_seen", "TEXT"),
                ("username",  "TEXT"),
            ],
            "runs": [
                ("finished_at", "TEXT"),
                ("exit_code",   "INTEGER"),
                ("log_path",    "TEXT"),
            ],
        }
        for table, cols in migrations.items():
            for col, coltype in cols:
                if not _col_exists(cur, table, col):
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
                    log.info(f"Migration: added {table}.{col}")

        conn.commit()

init_db()

# ═══════════════════════════════════════════════════════════════
#  DB HELPERS
# ═══════════════════════════════════════════════════════════════
def is_user_authenticated(user_id):
    cur = conn.cursor()
    cur.execute("SELECT is_auth FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return bool(row and row["is_auth"] == 1)

def upsert_user(user_id, username):
    with db_lock:
        cur = conn.cursor()
        now = datetime.utcnow().isoformat()
        cur.execute(
            "INSERT OR IGNORE INTO users (user_id, username, joined_at, last_seen, is_auth) VALUES (?,?,?,?,0)",
            (user_id, username or "", now, now)
        )
        cur.execute(
            "UPDATE users SET username=?, last_seen=? WHERE user_id=?",
            (username or "", now, user_id)
        )
        conn.commit()

def set_user_authenticated(user_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("UPDATE users SET is_auth=1 WHERE user_id=?", (user_id,))
        conn.commit()

def add_file_record(user_id, username, filename, orig_name, path, file_type, status="Pending"):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO files (user_id,username,filename,orig_name,path,uploaded_at,file_type,status) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, username, filename, orig_name, str(path),
             datetime.utcnow().isoformat(), file_type, status)
        )
        conn.commit()
        return cur.lastrowid

def list_user_files(user_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT id,filename,orig_name,uploaded_at,file_type,status,pid FROM files WHERE user_id=? AND status!='Rejected' ORDER BY id DESC",
        (user_id,)
    )
    return cur.fetchall()

def get_file_record(file_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM files WHERE id=?", (file_id,))
    return cur.fetchone()

def remove_file_record(file_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM files WHERE id=?", (file_id,))
        conn.commit()

def record_run_start(file_id, pid, log_path):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO runs (file_id,started_at,pid,log_path) VALUES (?,?,?,?)",
            (file_id, datetime.utcnow().isoformat(), pid, str(log_path))
        )
        conn.commit()
        return cur.lastrowid

def record_run_finish(run_id, exit_code):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE runs SET finished_at=?,exit_code=? WHERE id=?",
            (datetime.utcnow().isoformat(), exit_code, run_id)
        )
        conn.commit()

def update_file_status(file_id, pid, status):
    with db_lock:
        cur = conn.cursor()
        cur.execute("UPDATE files SET pid=?,status=? WHERE id=?", (pid, status, file_id))
        conn.commit()

# ═══════════════════════════════════════════════════════════════
#  PROCESS MANAGEMENT
# ═══════════════════════════════════════════════════════════════
PROCS     = {}   # file_id -> { process, run_id, log_path, started_at }
proc_lock = threading.Lock()

def get_system_load():
    if not PSUTIL_AVAILABLE:
        return 0.0, 0.0, len(PROCS)
    try:
        return (
            float(psutil.cpu_percent(interval=0.1)),
            float(psutil.virtual_memory().percent),
            len(PROCS),
        )
    except Exception:
        return 0.0, 0.0, 0

def should_stop_due_to_load():
    cpu, mem, count = get_system_load()
    if count >= MAX_RUNNING:
        return True, f"Too many processes ({count}/{MAX_RUNNING})"
    if PSUTIL_AVAILABLE and cpu >= CPU_THRESHOLD:
        return True, f"High CPU ({cpu:.1f}%)"
    if PSUTIL_AVAILABLE and mem >= MEMORY_THRESHOLD:
        return True, f"High memory ({mem:.1f}%)"
    return False, None

def get_file_type(filename):
    if not filename:
        return "unknown"
    fn = filename.lower()
    if fn.endswith(".py"):   return "python"
    if fn.endswith(".js"):   return "javascript"
    if fn.endswith(".zip"):  return "zip"
    if any(fn.endswith(x) for x in [".tar", ".tar.gz", ".tgz"]): return "archive"
    return "unknown"

def extract_archive(file_path, extract_dir):
    try:
        fp = str(file_path)
        if fp.endswith(".zip"):
            with zipfile.ZipFile(fp, "r") as z:
                z.extractall(extract_dir)
        elif fp.endswith((".tar.gz", ".tgz")):
            with tarfile.open(fp, "r:gz") as t:
                t.extractall(extract_dir)
        elif fp.endswith(".tar"):
            with tarfile.open(fp, "r") as t:
                t.extractall(extract_dir)
        else:
            return False, "Unsupported archive"
        return True, None
    except Exception as e:
        return False, str(e)

def find_main_file(directory):
    priority = ["main.py","bot.py","app.py","server.py","index.py","script.py",
                "main.js","bot.js","app.js","server.js","index.js","script.js"]
    for name in priority:
        p = Path(directory) / name
        if p.is_file():
            return str(p)
    for root, _, files in os.walk(directory):
        for name in priority:
            if name in files:
                return os.path.join(root, name)
    for root, _, files in os.walk(directory):
        for f in files:
            if f.endswith((".py", ".js")):
                return os.path.join(root, f)
    return None

def install_requirements_file(req_path, chat_id):
    try:
        with open(req_path) as f:
            pkgs = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        ok = fail = 0
        failed = []
        for pkg in pkgs:
            if "psutil" in pkg.lower():
                continue
            r = subprocess.run([sys.executable,"-m","pip","install",pkg,"-q"],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                ok += 1
            else:
                fail += 1
                failed.append(pkg)
        msg = f"📦 Installed {ok} packages"
        if failed:
            msg += f", failed: {', '.join(failed[:5])}"
        bot.send_message(chat_id, msg)
    except Exception as e:
        log.error(f"req install error: {e}")

def extract_imports(file_path):
    imports = set()
    try:
        with open(file_path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    imports.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    imports.add(node.module.split(".")[0])
    except Exception:
        pass
    return imports

def install_missing_imports(imports, chat_id):
    pip_map = {"telebot":"pyTelegramBotAPI","PIL":"Pillow","cv2":"opencv-python",
               "Crypto":"pycryptodome","bs4":"beautifulsoup4"}
    missing = []
    for m in imports:
        if m == "psutil":
            continue
        try:
            importlib.import_module(m)
        except ImportError:
            missing.append(m)
    if not missing:
        return
    ok = fail = 0
    for m in missing:
        pkg = pip_map.get(m, m)
        r = subprocess.run([sys.executable,"-m","pip","install",pkg,"-q"],
                           capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            ok += 1
        else:
            fail += 1
    if ok or fail:
        bot.send_message(chat_id, f"📦 Auto-installed {ok} deps, failed {fail}")

def start_file_process(file_id, chat_id):
    should_stop, reason = should_stop_due_to_load()
    if should_stop:
        bot.send_message(chat_id, f"⚠️ Cannot start: {reason}")
        return

    rec = get_file_record(file_id)
    if not rec:
        bot.send_message(chat_id, "❌ File record not found")
        return

    file_path = Path(rec["path"])
    orig_name = rec["orig_name"]

    if file_path.is_dir():
        target = find_main_file(str(file_path))
        if not target:
            bot.send_message(chat_id, "❌ No main file found in archive")
            return
        working_dir = str(Path(target).parent)
    else:
        target      = str(file_path)
        working_dir = str(file_path.parent)

    ext = Path(target).suffix.lower()

    if ext == ".py":
        req = Path(working_dir) / "requirements.txt"
        if req.exists():
            bot.send_message(chat_id, "📦 Installing requirements.txt...")
            install_requirements_file(str(req), chat_id)
        imports = extract_imports(target)
        if imports:
            install_missing_imports(imports, chat_id)
        cmd = [sys.executable, "-u", target]
    elif ext == ".js":
        cmd = ["node", target]
    else:
        bot.send_message(chat_id, f"❌ Unsupported: {ext}")
        return

    log_path = LOGS_DIR / f"file_{file_id}_{int(time.time())}.log"

    try:
        log_file = open(str(log_path), "w")
        process  = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=working_dir,
            text=True,
        )
        run_id = record_run_start(file_id, process.pid, log_path)
        update_file_status(file_id, process.pid, "Running")

        with proc_lock:
            PROCS[file_id] = {
                "process":    process,
                "run_id":     run_id,
                "log_path":   str(log_path),
                "started_at": datetime.utcnow().isoformat(),
                "log_file":   log_file,
            }

        bot.send_message(chat_id, f"✅ <b>{html_lib.escape(orig_name)}</b> started!\nPID: <code>{process.pid}</code>")

        def monitor():
            try:
                code = process.wait()
            except Exception:
                code = -1
            finally:
                try:
                    log_file.close()
                except Exception:
                    pass
                update_file_status(file_id, None, "Stopped")
                record_run_finish(run_id, code)
                with proc_lock:
                    PROCS.pop(file_id, None)

        threading.Thread(target=monitor, daemon=True).start()
    except Exception as e:
        bot.send_message(chat_id, f"❌ Start failed: {e}")

def stop_file_process(file_id):
    with proc_lock:
        info = PROCS.pop(file_id, None)
    if not info:
        return False
    proc = info["process"]
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    except Exception as e:
        log.error(f"stop error: {e}")
    try:
        info.get("log_file", None) and info["log_file"].close()
    except Exception:
        pass
    update_file_status(file_id, None, "Stopped")
    return True

def get_file_logs(file_id, lines=50):
    try:
        with proc_lock:
            info = PROCS.get(file_id)
        if info:
            log_path = info["log_path"]
            if os.path.exists(log_path):
                with open(log_path) as f:
                    content = f.readlines()
                return "".join(content[-lines:]) or "No output yet"

        cur = conn.cursor()
        cur.execute("SELECT log_path FROM runs WHERE file_id=? ORDER BY started_at DESC LIMIT 1", (file_id,))
        row = cur.fetchone()
        if row and row[0] and os.path.exists(row[0]):
            with open(row[0]) as f:
                content = f.readlines()
            return "".join(content[-lines:]) or "Empty log"
        return "No log file found"
    except Exception as e:
        return f"Log error: {e}"

# ═══════════════════════════════════════════════════════════════
#  BOT INSTANCE
# ═══════════════════════════════════════════════════════════════
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ── KEYBOARDS ─────────────────────────────────────────────────
def main_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("📢 Updates Channel"))
    kb.add(KeyboardButton("📤 Upload File"), KeyboardButton("📁 My Files"))
    kb.add(KeyboardButton("⚡ Bot Speed"),   KeyboardButton("📊 Statistics"))
    kb.add(KeyboardButton("📞 Contact Owner"))
    return kb

def file_actions_kb(file_id, running=False):
    kb = InlineKeyboardMarkup()
    if running:
        kb.row(
            InlineKeyboardButton("⏹ Stop",    callback_data=f"stop:{file_id}"),
            InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}"),
        )
    else:
        kb.row(
            InlineKeyboardButton("▶️ Start",   callback_data=f"start:{file_id}"),
            InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}"),
        )
    kb.row(
        InlineKeyboardButton("🗑 Delete", callback_data=f"delete:{file_id}"),
        InlineKeyboardButton("📄 Logs",   callback_data=f"logs:{file_id}"),
    )
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_to_files"))
    return kb

# ── HELPERS ───────────────────────────────────────────────────
def send_files_list(chat_id, user_id):
    files = list_user_files(user_id)
    if not files:
        bot.send_message(chat_id, "📁 <b>Your Files</b>\n\nNo files uploaded yet.")
        return
    kb = InlineKeyboardMarkup()
    for row in files:
        fid, fname, orig, uploaded, ftype, status, pid = row
        icon = "🟢" if status == "Running" else ("⏳" if status == "Pending" else "🔴")
        kb.add(InlineKeyboardButton(f"{icon} {orig} ({ftype})", callback_data=f"manage:{fid}"))
    bot.send_message(chat_id, "📁 <b>Your Files</b>\n\nClick to manage:", reply_markup=kb)

def show_file_management(chat_id, file_id, user_id, message_id=None):
    rec = get_file_record(file_id)
    if not rec or rec["user_id"] != user_id:
        bot.send_message(chat_id, "❌ Access denied or file not found")
        return
    with proc_lock:
        running = file_id in PROCS
    status = rec["status"]
    if running:
        status_text = "🟢 Running"
    elif status == "Pending":
        status_text = "⏳ Pending Approval"
    else:
        status_text = "🔴 Stopped"

    text = (
        f"⚙️ <b>File Management</b>\n\n"
        f"📁 File: {html_lib.escape(rec['orig_name'])}\n"
        f"📊 Type: {rec['file_type']}\n"
        f"📈 Status: {status_text}\n"
        f"⏰ Uploaded: {(rec['uploaded_at'] or '')[:16]}"
    )
    kb = file_actions_kb(file_id, running)
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
            return
        except Exception:
            pass
    bot.send_message(chat_id, text, reply_markup=kb)

# ═══════════════════════════════════════════════════════════════
#  HANDLERS
# ═══════════════════════════════════════════════════════════════
@bot.message_handler(commands=["start", "help"])
def start_handler(message):
    user    = message.from_user
    user_id = user.id
    upsert_user(user_id, user.username)

    if not is_user_authenticated(user_id) and user_id != ADMIN_ID:
        bot.send_message(message.chat.id,
            "🔐 <b>Password required to access this bot.</b>\n\nSend the password:")
        return

    if user_id == ADMIN_ID and not is_user_authenticated(user_id):
        set_user_authenticated(user_id)

    files = list_user_files(user_id)
    bot.send_message(
        message.chat.id,
        f"🔥 <b>24x7 LAZZYXMOD HOSTING BOT</b>\n\n"
        f"👋 Welcome <b>{html_lib.escape(user.first_name or 'User')}</b>\n"
        f"🆔 Your ID: <code>{user_id}</code>\n"
        f"📂 Files: {len(files)}/{MAX_FILES_PER_USER}\n\n"
        "👇 Use buttons below to get started!",
        reply_markup=main_menu_kb(),
    )


@bot.message_handler(func=lambda m: True, content_types=["text"])
def text_handler(message):
    user_id = message.from_user.id
    text    = message.text.strip()
    upsert_user(user_id, message.from_user.username)

    if not is_user_authenticated(user_id) and user_id != ADMIN_ID:
        if text == SECRET_PASSWORD:
            set_user_authenticated(user_id)
            bot.send_message(message.chat.id, "✅ <b>Access Granted!</b>")
            start_handler(message)
        else:
            bot.send_message(message.chat.id, "❌ Wrong password. Try again.")
        return

    if text == "📢 Updates Channel":
        bot.send_message(message.chat.id, "📢 Channel: t.me/lazzyxprivet")
    elif text == "📞 Contact Owner":
        bot.send_message(message.chat.id, "📞 Contact: @lazzyxmod")
    elif text == "⚡ Bot Speed":
        cpu, mem, procs = get_system_load()
        td   = datetime.utcnow() - START_TIME
        days = td.days
        h, r = divmod(td.seconds, 3600)
        m, s = divmod(r, 60)
        bot.send_message(message.chat.id,
            f"⚡ <b>System Status</b>\n\n"
            f"• CPU: {cpu:.1f}%\n"
            f"• Memory: {mem:.1f}%\n"
            f"• Running Processes: {procs}\n"
            f"• Max: {MAX_RUNNING}\n"
            f"• Uptime: {days}d {h}h {m}m"
        )
    elif text == "📊 Statistics":
        cur = conn.cursor()
        cur.execute("SELECT COUNT(DISTINCT user_id) FROM files")
        users = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(*) FROM files")
        files = cur.fetchone()[0] or 0
        cur.execute("SELECT COUNT(*) FROM files WHERE status='Running'")
        running = cur.fetchone()[0] or 0
        cpu, mem, _ = get_system_load()
        bot.send_message(message.chat.id,
            f"📊 <b>Bot Statistics</b>\n\n"
            f"👥 Total Users: {users}\n"
            f"📁 Total Files: {files}\n"
            f"🚀 Running: {running}\n"
            f"⚡ CPU: {cpu:.1f}%\n"
            f"💾 Memory: {mem:.1f}%"
        )
    elif text == "📁 My Files":
        send_files_list(message.chat.id, user_id)
    elif text == "📤 Upload File":
        bot.send_message(message.chat.id,
            "📤 <b>Upload a File</b>\n\n"
            "Send me a <b>.py</b>, <b>.js</b> file, or a <b>ZIP / TAR</b> archive."
        )
    else:
        bot.send_message(message.chat.id, "❓ Use the menu buttons below.", reply_markup=main_menu_kb())


@bot.message_handler(content_types=["document"])
def document_handler(message):
    user_id = message.from_user.id
    upsert_user(user_id, message.from_user.username)

    if not is_user_authenticated(user_id) and user_id != ADMIN_ID:
        bot.send_message(message.chat.id, "🔐 Send the password first.")
        return

    user_files = list_user_files(user_id)
    if len(user_files) >= MAX_FILES_PER_USER:
        bot.reply_to(message, f"❌ File limit reached ({MAX_FILES_PER_USER}). Delete some first.")
        return

    try:
        file_info  = bot.get_file(message.document.file_id)
        file_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.reply_to(message, f"❌ Download failed: {e}")
        return

    orig_name = message.document.file_name or "unknown"
    file_type = get_file_type(orig_name)
    user_dir  = UPLOADS_DIR / str(user_id)
    user_dir.mkdir(exist_ok=True)

    safe_name = f"{int(time.time())}_{orig_name}"
    file_path = user_dir / safe_name

    try:
        file_path.write_bytes(file_bytes)
    except Exception as e:
        bot.reply_to(message, f"❌ Save failed: {e}")
        return

    final_path = file_path

    if file_type in ("zip", "archive"):
        bot.reply_to(message, "📦 Extracting archive...")
        extract_dir = TEMP_DIR / f"ext_{user_id}_{int(time.time())}"
        extract_dir.mkdir(parents=True, exist_ok=True)
        ok, err = extract_archive(file_path, str(extract_dir))
        if not ok:
            bot.reply_to(message, f"❌ Extraction failed: {err}")
            return
        main = find_main_file(str(extract_dir))
        if not main:
            bot.reply_to(message, "❌ No main script found in archive.")
            return
        final_path = extract_dir
        file_type  = get_file_type(main)

    file_id = add_file_record(
        user_id, message.from_user.username or "",
        safe_name, orig_name, str(final_path), file_type, "Pending"
    )

    bot.reply_to(message, "⏳ <b>Sent to admin for approval. You'll be notified!</b>")

    admin_kb = InlineKeyboardMarkup()
    admin_kb.row(
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{file_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject:{file_id}"),
    )
    uname = message.from_user.username or "NoUsername"
    try:
        bot.send_message(
            ADMIN_ID,
            f"🚨 <b>New File Pending Approval</b>\n\n"
            f"👤 @{uname}  (<code>{user_id}</code>)\n"
            f"📄 File: <code>{html_lib.escape(orig_name)}</code>\n"
            f"📊 Type: {file_type}",
            reply_markup=admin_kb,
        )
    except Exception as e:
        log.error(f"Admin alert failed: {e}")


# ═══════════════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda c: True)
def callback_handler(call):
    data    = call.data
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    msg_id  = call.message.message_id

    if not is_user_authenticated(user_id) and user_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Unauthorized!", show_alert=True)
        return

    bot.answer_callback_query(call.id)

    try:
        if data.startswith("approve:"):
            if user_id != ADMIN_ID:
                bot.answer_callback_query(call.id, "Admin only!", show_alert=True)
                return
            file_id = int(data.split(":")[1])
            rec = get_file_record(file_id)
            if rec:
                update_file_status(file_id, None, "Stopped")
                bot.edit_message_text(f"✅ Approved file ID: {file_id}", chat_id, msg_id)
                start_file_process(file_id, rec["user_id"])
                try:
                    bot.send_message(
                        rec["user_id"],
                        f"🎉 <b>'{html_lib.escape(rec['orig_name'])}' approved and started!</b>",
                    )
                except Exception:
                    pass
            return

        if data.startswith("reject:"):
            if user_id != ADMIN_ID:
                bot.answer_callback_query(call.id, "Admin only!", show_alert=True)
                return
            file_id = int(data.split(":")[1])
            rec = get_file_record(file_id)
            if rec:
                update_file_status(file_id, None, "Rejected")
                bot.edit_message_text(f"❌ Rejected file ID: {file_id}", chat_id, msg_id)
                try:
                    bot.send_message(
                        rec["user_id"],
                        f"❌ <b>'{html_lib.escape(rec['orig_name'])}' was rejected by admin.</b>",
                    )
                except Exception:
                    pass
            return

        if data == "back_to_files":
            try:
                bot.delete_message(chat_id, msg_id)
            except Exception:
                pass
            send_files_list(chat_id, user_id)
            return

        if data.startswith("manage:"):
            file_id = int(data.split(":")[1])
            show_file_management(chat_id, file_id, user_id, msg_id)

        elif data.startswith("start:"):
            file_id = int(data.split(":")[1])
            start_file_process(file_id, chat_id)
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, msg_id)

        elif data.startswith("stop:"):
            file_id = int(data.split(":")[1])
            stop_file_process(file_id)
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, msg_id)

        elif data.startswith("restart:"):
            file_id = int(data.split(":")[1])
            stop_file_process(file_id)
            time.sleep(2)
            start_file_process(file_id, chat_id)
            time.sleep(1)
            show_file_management(chat_id, file_id, user_id, msg_id)

        elif data.startswith("delete:"):
            file_id = int(data.split(":")[1])
            rec = get_file_record(file_id)
            if rec:
                stop_file_process(file_id)
                p = Path(rec["path"])
                try:
                    if p.is_dir():
                        shutil.rmtree(str(p), ignore_errors=True)
                    elif p.exists():
                        p.unlink()
                except Exception as e:
                    log.error(f"delete error: {e}")
                remove_file_record(file_id)
            send_files_list(chat_id, user_id)

        elif data.startswith("logs:"):
            file_id = int(data.split(":")[1])
            logs = get_file_logs(file_id)
            rec  = get_file_record(file_id)
            name = rec["orig_name"] if rec else "Unknown"
            if len(logs) > 4000:
                logs = "...(truncated)\n" + logs[-4000:]
            bot.send_message(
                chat_id,
                f"📄 <b>Logs — {html_lib.escape(name)}</b>\n\n<pre>{html_lib.escape(logs)}</pre>",
            )

    except Exception as e:
        log.error(f"Callback error [{data}]: {e}")
        bot.send_message(chat_id, f"❌ Error: {html_lib.escape(str(e))}")


# ═══════════════════════════════════════════════════════════════
#  MAIN — infinity polling with crash recovery
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    log.info("✅ Bot starting — 24/7 mode ON")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=50)
        except Exception as e:
            log.error(f"Polling crash: {e} — restarting in 5s")
            time.sleep(5)