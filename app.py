import os
import json
import logging
import uuid
import subprocess
import shutil
import shlex
import threading
import time
import urllib.request
import smtplib
import secrets
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import Flask, render_template, jsonify, request, session, flash, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash

# ==================== 1. FLASK UYGULAMASI VE LOG TANIMLARI ====================
app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "discell_super_secret_safe_key_2026")

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Dosya ve Klasör Yapısı
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_FILE = os.path.join(DATA_DIR, 'db.json')
SERVERS_DIR = os.path.join(BASE_DIR, 'servers_data')

# Aktif çalışan Node.js süreçlerini RAM'de tutacağımız sözlük
active_processes = {}

# Kullanıcı başına maksimum sanal sunucu sayısı
MAX_SERVERS_PER_USER = 3

# Email doğrulama ayarları
APP_BASE_URL = os.getenv("APP_BASE_URL", "https://vds-virtual-server.onrender.com").rstrip("/")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

# Birkaç farklı env adı destekleniyor
SMTP_EMAIL = (
    os.getenv("SMTP_EMAIL")
    or os.getenv("GMAIL_EMAIL")
    or os.getenv("EMAIL_ADDRESS")
    or ""
).strip()

SMTP_APP_PASSWORD = (
    os.getenv("SMTP_APP_PASSWORD")
    or os.getenv("GMAIL_APP_PASSWORD")
    or os.getenv("GMAIL_PASSWORD")
    or os.getenv("EMAIL_APP_PASSWORD")
    or ""
).strip()

EMAIL_VERIFICATION_REQUIRED = True
VERIFICATION_TOKEN_EXPIRY_SECONDS = 24 * 60 * 60


# ==================== 2. VERİTABANI VE YARDIMCI FONKSİYONLAR ====================

def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(SERVERS_DIR, exist_ok=True)
    if not os.path.exists(DB_FILE):
        default_structure = {"settings": {}, "users": {}, "servers": {}, "logs": []}
        with open(DB_FILE, 'w', encoding='utf-8') as f:
            json.dump(default_structure, f, indent=4, ensure_ascii=False)


def load_data():
    init_db()
    try:
        with open(DB_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {"settings": {}, "users": {}, "servers": {}, "logs": []}


def save_data(data):
    with open(DB_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4, ensure_ascii=False)


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def ensure_user_defaults(user: dict):
    if not isinstance(user, dict):
        return {}
    if "verified" not in user:
        user["verified"] = True  # Eski kullanıcılar için uyumluluk
    if "verify_token" not in user:
        user["verify_token"] = ""
    if "verify_token_created_at" not in user:
        user["verify_token_created_at"] = 0
    return user


def get_current_user_record(data):
    email = normalize_email(session.get("email"))
    if not email:
        return None
    user = data.get("users", {}).get(email)
    if not user:
        return None
    return ensure_user_defaults(user)


def is_user_verified(user: dict) -> bool:
    if not user:
        return False
    return bool(user.get("verified", True))


def verify_password(stored_password: str, provided_password: str) -> bool:
    if not stored_password:
        return False

    try:
        if check_password_hash(stored_password, provided_password):
            return True
    except Exception:
        pass

    # Eski düz metin şifrelerle uyumluluk
    return stored_password == provided_password


def get_user_server_count(data, email):
    email = normalize_email(email)
    return sum(1 for srv in data.get('servers', {}).values() if normalize_email(srv.get('owner')) == email)


def can_access_server(server: dict, email: str) -> bool:
    email = normalize_email(email)
    if not server:
        return False

    owner = normalize_email(server.get('owner'))
    collaborators = [normalize_email(x) for x in server.get('collaborators', [])]

    return email == owner or email in collaborators


def can_manage_server(server: dict, email: str) -> bool:
    return can_access_server(server, email)


def can_share_server(server: dict, email: str) -> bool:
    return normalize_email(server.get('owner')) == normalize_email(email)


def is_allowed_npm_command(command: str) -> bool:
    cmd = (command or "").strip()

    dangerous_chars = [';', '&', '|', '>', '<', '$', '`']
    if any(char in cmd for char in dangerous_chars):
        return False

    allowed_prefixes = (
        "npm install",
        "npm i",
        "npm update",
        "npm uninstall",
        "npm remove",
    )
    return any(cmd == prefix or cmd.startswith(prefix + " ") for prefix in allowed_prefixes)


def sanitize_package_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not name:
        return "my-server"
    safe = []
    for ch in name:
        if ch.isalnum() or ch in ['-', '_']:
            safe.append(ch)
        elif ch in [' ', '.']:
            safe.append('-')
    result = ''.join(safe).strip('-_')
    return result or "my-server"


def ensure_package_json_exists(server_path: str, server_name: str = "my-server") -> str:
    package_json_path = os.path.join(server_path, 'package.json')

    if not os.path.exists(package_json_path):
        default_package_json = {
            "name": sanitize_package_name(server_name),
            "version": "1.0.0",
            "description": "",
            "main": "index.js",
            "scripts": {
                "start": "node index.js"
            },
            "keywords": [],
            "author": "",
            "license": "ISC",
            "dependencies": {},
            "devDependencies": {}
        }

        with open(package_json_path, 'w', encoding='utf-8') as f:
            json.dump(default_package_json, f, indent=2, ensure_ascii=False)

    return package_json_path


def run_npm_command(server_path: str, command: str, log_file_path: str, server_name: str = "my-server"):
    args = shlex.split(command)

    if not args:
        raise ValueError("Komut boş olamaz.")

    if args[0] != "npm":
        raise ValueError("Sadece npm komutları destekleniyor.")

    ensure_package_json_exists(server_path, server_name)

    with open(log_file_path, 'a', encoding='utf-8') as log_file:
        log_file.write(f"\n[CMD] {command}\n")
        log_file.flush()
        subprocess.Popen(
            args,
            cwd=server_path,
            stdout=log_file,
            stderr=subprocess.STDOUT
        )


def stop_active_server(server_id):
    if server_id in active_processes:
        proc_info = active_processes[server_id]
        try:
            proc = proc_info.get("process")
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
        except Exception:
            pass

        try:
            log_file = proc_info.get("log_file")
            if log_file:
                log_file.close()
        except Exception:
            pass

        del active_processes[server_id]


def auto_install_dependencies_if_needed(server_path: str, log_file, server_name: str = "my-server"):
    package_json_path = ensure_package_json_exists(server_path, server_name)
    node_modules_path = os.path.join(server_path, 'node_modules')

    if os.path.exists(package_json_path) and not os.path.exists(node_modules_path):
        log_file.write("\n[Auto] package.json bulundu/oluşturuldu. npm install başlatılıyor...\n")
        log_file.flush()

        result = subprocess.run(
            ["npm", "install"],
            cwd=server_path,
            stdout=log_file,
            stderr=subprocess.STDOUT
        )

        if result.returncode != 0:
            raise RuntimeError("Otomatik npm install başarısız oldu.")


def is_safe_relative_path(rel_path: str) -> bool:
    rel_path = (rel_path or "").strip().replace("\\", "/")
    if not rel_path:
        return False
    if rel_path.startswith("/"):
        return False

    parts = [p for p in rel_path.split("/") if p not in ("", ".")]
    if not parts:
        return False
    if any(part == ".." for part in parts):
        return False
    if "node_modules" in parts:
        return False
    return True


def ensure_server_defaults(server: dict):
    if not isinstance(server, dict):
        return {}

    if "collaborators" not in server or not isinstance(server.get("collaborators"), list):
        server["collaborators"] = []
    if "main_file" not in server:
        server["main_file"] = "index.js"
    if "ram" not in server:
        server["ram"] = "1 GB"
    if "cpu" not in server:
        server["cpu"] = "1 vCPU"
    return server


def generate_verification_token() -> str:
    return secrets.token_urlsafe(32)


def find_user_by_verification_token(data, token: str):
    token = (token or "").strip()
    if not token:
        return None, None

    for email, user in data.get("users", {}).items():
        user = ensure_user_defaults(user)
        if user.get("verify_token") == token:
            return email, user
    return None, None


def mail_config_ready() -> bool:
    return bool(SMTP_EMAIL and SMTP_APP_PASSWORD)


def send_verification_email(to_email: str, username: str, token: str):
    if not mail_config_ready():
        raise RuntimeError(
            "Mail ayarları eksik. SMTP_EMAIL/SMTP_APP_PASSWORD veya GMAIL_EMAIL/GMAIL_APP_PASSWORD tanımlanmalı."
        )

    verify_link = f"{APP_BASE_URL}/verify-email/{token}"

    subject = "Discell hesabını doğrula"
    plain_text = (
        f"Merhaba {username},\n\n"
        f"Discell hesabını doğrulamak için şu bağlantıya tıkla:\n"
        f"{verify_link}\n\n"
        f"Bu bağlantı 24 saat geçerlidir.\n"
    )

    html_text = f"""
    <html>
      <body style="font-family: Arial, sans-serif; background:#0b0c10; color:#f2f3f5; padding:20px;">
        <div style="max-width:600px; margin:0 auto; background:#14161d; border-radius:16px; padding:24px; border:1px solid rgba(255,255,255,0.08);">
          <h2 style="margin-top:0; color:#00f2fe;">Discell Hesap Doğrulama</h2>
          <p>Merhaba <strong>{username}</strong>,</p>
          <p>Hesabını doğrulamak için aşağıdaki butona tıkla.</p>
          <p style="margin:28px 0;">
            <a href="{verify_link}" style="background:linear-gradient(135deg,#7b2cbf,#9b51e0); color:#fff; text-decoration:none; padding:12px 18px; border-radius:10px; display:inline-block;">
              Hesabımı Doğrula
            </a>
          </p>
          <p>Bağlantı çalışmazsa bunu tarayıcıya yapıştır:</p>
          <p style="word-break:break-all; color:#00f2fe;">{verify_link}</p>
          <p style="color:#a3a7ae; font-size:13px;">Bu bağlantı 24 saat geçerlidir.</p>
        </div>
      </body>
    </html>
    """

    msg = MIMEMultipart("alternative")
    msg["From"] = SMTP_EMAIL
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(plain_text, "plain", "utf-8"))
    msg.attach(MIMEText(html_text, "html", "utf-8"))

    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as smtp:
        smtp.starttls()
        smtp.login(SMTP_EMAIL, SMTP_APP_PASSWORD)
        smtp.sendmail(SMTP_EMAIL, [to_email], msg.as_string())


# ==================== 3. OTOMATİK KURTARMA VE CANLI TUTMA MEKANİZMASI ====================

def keep_alive_daemon():
    """Her 5 saniyede bir kendi URL'sine istek atar."""
    url = APP_BASE_URL + "/"
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) KeepAlive/1.0'}

    time.sleep(3)

    while True:
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req) as response:
                if response.status == 200:
                    print(f"[CANLI-TUTMA] {time.strftime('%Y-%m-%d %H:%M:%S')} - Sunucu başarıyla tetiklendi (200 OK)", flush=True)
        except Exception as e:
            print(f"[CANLI-TUTMA] Ping hatası oluştu: {e}", flush=True)
        time.sleep(5)


def resume_all_running_servers():
    """Render reset attığında, durumu 'Çalışıyor' olan botları otomatik tekrar başlatır."""
    logging.info("[AUTO-RESUME] Çalışması gereken botlar kontrol ediliyor...")
    data = load_data()
    for server_id, server_data in data.get('servers', {}).items():
        if server_data.get('status') == 'Çalışıyor':
            server_path = os.path.join(SERVERS_DIR, server_id)
            main_file = server_data.get('main_file', 'index.js')
            target_file = os.path.join(server_path, main_file)
            log_file_path = os.path.join(server_path, 'server_output.log')

            if os.path.exists(target_file) and server_id not in active_processes:
                try:
                    log_file = open(log_file_path, 'a', encoding='utf-8')
                    log_file.write("\n[SİSTEM] Sunucu yeniden başladı, bot otomatik kurtarıldı.\n")
                    log_file.flush()

                    process = subprocess.Popen(
                        ['node', main_file],
                        cwd=server_path,
                        stdin=subprocess.PIPE,
                        stdout=log_file,
                        stderr=subprocess.STDOUT
                    )

                    active_processes[server_id] = {
                        "process": process,
                        "log_file": log_file
                    }
                    logging.info(f"[AUTO-RESUME] {server_data.get('name')} ({server_id}) başarıyla kurtarıldı.")
                except Exception as e:
                    logging.error(f"[AUTO-RESUME] {server_id} başlatılamadı: {e}")


threading.Thread(target=keep_alive_daemon, daemon=True).start()


# ==================== 4. FLASK URL / WEB ROTALARI ====================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = (request.form.get('username') or "").strip()
        email = normalize_email(request.form.get('email'))
        password = request.form.get('password') or ""

        if not username or not email or not password:
            flash('Tüm alanları doldurmalısın.', 'danger')
            return redirect(url_for('register'))

        data = load_data()

        if email in data['users']:
            flash('Bu e-posta adresi zaten kayıtlı!', 'danger')
            return redirect(url_for('register'))

        verify_token = generate_verification_token()

        data['users'][email] = {
            "username": username,
            "password": generate_password_hash(password),
            "verified": False,
            "verify_token": verify_token,
            "verify_token_created_at": time.time()
        }
        save_data(data)

        try:
            if mail_config_ready():
                send_verification_email(email, username, verify_token)
                flash('Hesabın oluşturuldu! Gmail adresine doğrulama bağlantısı gönderildi.', 'success')
            else:
                flash(
                    'Hesabın oluşturuldu, fakat mail ayarları eksik olduğu için doğrulama maili gönderilemedi. '
                    'SMTP_EMAIL ve SMTP_APP_PASSWORD veya GMAIL_EMAIL ve GMAIL_APP_PASSWORD ayarla.',
                    'warning'
                )
        except Exception as e:
            flash(
                f'Hesabın oluşturuldu ama doğrulama maili gönderilemedi: {str(e)}',
                'danger'
            )

        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/verify-email/<token>')
def verify_email(token):
    data = load_data()
    email, user = find_user_by_verification_token(data, token)

    if not user:
        flash('Doğrulama bağlantısı geçersiz.', 'danger')
        return redirect(url_for('login'))

    created_at = float(user.get("verify_token_created_at", 0) or 0)
    if created_at and (time.time() - created_at) > VERIFICATION_TOKEN_EXPIRY_SECONDS:
        flash('Doğrulama bağlantısının süresi dolmuş.', 'warning')
        return redirect(url_for('login'))

    user["verified"] = True
    user["verify_token"] = ""
    user["verify_token_created_at"] = 0
    data["users"][email] = user
    save_data(data)

    flash('Hesabın doğrulandı. Şimdi giriş yapabilirsin.', 'success')
    return redirect(url_for('login'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        password = request.form.get('password') or ""

        data = load_data()

        if email in data['users']:
            user = ensure_user_defaults(data['users'][email])
            stored_password = user.get('password', '')

            if verify_password(stored_password, password):
                if stored_password == password:
                    user["password"] = generate_password_hash(password)
                    data["users"][email] = user
                    save_data(data)

                if EMAIL_VERIFICATION_REQUIRED and not user.get("verified", True):
                    flash('Hesabın doğrulanmamış. Gmail kutunu kontrol et.', 'warning')
                    return redirect(url_for('login'))

                session['email'] = email
                session['username'] = user.get('username', '')
                flash('Başarıyla giriş yapıldı.', 'success')
                return redirect(url_for('dashboard_menu'))

        flash('Hatalı e-posta veya şifre girdiniz!', 'danger')
        return redirect(url_for('login'))

    return render_template('login.html')


@app.route('/dashboard-menu')
def dashboard_menu():
    if 'username' not in session:
        return redirect(url_for('login'))

    data = load_data()
    current_user = get_current_user_record(data)
    if EMAIL_VERIFICATION_REQUIRED and not is_user_verified(current_user):
        flash('Paneli kullanmak için hesabını önce doğrulamalısın.', 'warning')
        return redirect(url_for('login'))

    current_email = normalize_email(session['email'])

    user_servers = {}
    for k, v in data.get('servers', {}).items():
        v = ensure_server_defaults(v)
        if can_access_server(v, current_email):
            v["is_owner"] = normalize_email(v.get("owner")) == current_email
            user_servers[k] = v

    for srv_id, srv_data in user_servers.items():
        if srv_data.get('status') == 'Çalışıyor' and srv_id not in active_processes:
            srv_data['status'] = 'Durduruldu'
            data['servers'][srv_id] = srv_data

    save_data(data)
    return render_template('dashboardmenu.html', servers=user_servers)


@app.route('/create-server', methods=['POST'])
def create_server():
    if 'username' not in session:
        return redirect(url_for('login'))

    data = load_data()
    current_user = get_current_user_record(data)
    if EMAIL_VERIFICATION_REQUIRED and not is_user_verified(current_user):
        flash('Sunucu oluşturmak için hesabını önce doğrulamalısın.', 'warning')
        return redirect(url_for('login'))

    server_name = (request.form.get('server_name') or "").strip()[:16]
    bot_token = request.form.get('bot_token', '')

    if not server_name:
        flash('Sunucu adı boş olamaz.', 'danger')
        return redirect(url_for('dashboard_menu'))

    current_count = get_user_server_count(data, session['email'])
    if current_count >= MAX_SERVERS_PER_USER:
        flash(f'En fazla {MAX_SERVERS_PER_USER} sanal sunucu oluşturabilirsiniz.', 'danger')
        return redirect(url_for('dashboard_menu'))

    server_id = str(uuid.uuid4())[:8]
    data['servers'][server_id] = {
        "id": server_id,
        "owner": normalize_email(session['email']),
        "name": server_name,
        "token": bot_token,
        "status": "Durduruldu",
        "main_file": "index.js",
        "collaborators": [],
        "ram": "1 GB",
        "cpu": "1 vCPU"
    }
    save_data(data)

    server_path = os.path.join(SERVERS_DIR, server_id)
    os.makedirs(server_path, exist_ok=True)
    os.makedirs(os.path.join(server_path, 'data'), exist_ok=True)

    ensure_package_json_exists(server_path, server_name)

    default_code = (
        f"// {server_name} - Ana Dosya\n"
        f"console.log('Bot başlatılıyor...');\n"
        f"setInterval(() => {{ console.log('Bot aktif...'); }}, 60000);\n"
    )

    with open(os.path.join(server_path, 'index.js'), 'w', encoding='utf-8') as f:
        f.write(default_code)

    flash(f'"{server_name}" isimli gerçek sanal ortam oluşturuldu!', 'success')
    return redirect(url_for('dashboard_menu'))


@app.route('/dashboard/<server_id>/delete', methods=['POST'])
def delete_server(server_id):
    if 'username' not in session:
        return redirect(url_for('login'))

    data = load_data()
    current_user = get_current_user_record(data)
    if EMAIL_VERIFICATION_REQUIRED and not is_user_verified(current_user):
        flash('Bu işlemi yapmak için hesabını doğrulamalısın.', 'warning')
        return redirect(url_for('login'))

    server = ensure_server_defaults(data.get('servers', {}).get(server_id))

    if not server or normalize_email(server.get('owner')) != normalize_email(session['email']):
        flash('Silme işlemi için yetkiniz yok.', 'danger')
        return redirect(url_for('dashboard_menu'))

    stop_active_server(server_id)

    server_path = os.path.join(SERVERS_DIR, server_id)
    if os.path.exists(server_path):
        shutil.rmtree(server_path, ignore_errors=True)

    if server_id in data.get('servers', {}):
        del data['servers'][server_id]

    save_data(data)
    flash('Sanal sunucu başarıyla silindi.', 'success')
    return redirect(url_for('dashboard_menu'))


@app.route('/dashboard/<server_id>/read-file', methods=['GET'])
def read_file(server_id):
    if 'username' not in session:
        return jsonify({"status": "error", "message": "Oturum açmanız gerekiyor."}), 403

    data = load_data()
    current_user = get_current_user_record(data)
    if EMAIL_VERIFICATION_REQUIRED and not is_user_verified(current_user):
        return jsonify({"status": "error", "message": "Hesabınızı doğrulamanız gerekiyor."}), 403

    server = ensure_server_defaults(data.get('servers', {}).get(server_id))
    if not server or not can_access_server(server, session['email']):
        return jsonify({"status": "error", "message": "Yetkisiz erişim."}), 403

    filename = request.args.get('file', '').strip()

    if not is_safe_relative_path(filename):
        return jsonify({"status": "error", "message": "Geçersiz dosya yolu."}), 400

    safe_path = os.path.join(SERVERS_DIR, server_id, filename)

    if not os.path.exists(safe_path) or os.path.isdir(safe_path):
        return jsonify({"status": "error", "message": "Dosya bulunamadı."}), 404

    try:
        with open(safe_path, 'r', encoding='utf-8') as f:
            return jsonify({"status": "success", "content": f.read()})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/server/<server_id>/logs', methods=['GET'])
def get_logs(server_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 403

    data = load_data()
    current_user = get_current_user_record(data)
    if EMAIL_VERIFICATION_REQUIRED and not is_user_verified(current_user):
        return jsonify({'error': 'Unauthorized'}), 403

    server = ensure_server_defaults(data.get('servers', {}).get(server_id))
    if not server or not can_access_server(server, session['email']):
        return jsonify({'error': 'Unauthorized'}), 403

    log_path = os.path.join(SERVERS_DIR, server_id, 'server_output.log')
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            return jsonify({'logs': "".join(lines[-100:])})

    return jsonify({'logs': 'Henüz log yok veya sunucu kapalı.'})


@app.route('/api/server/<server_id>/command', methods=['POST'])
def send_command(server_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 403

    data = load_data()
    current_user = get_current_user_record(data)
    if EMAIL_VERIFICATION_REQUIRED and not is_user_verified(current_user):
        return jsonify({'error': 'Unauthorized'}), 403

    server = ensure_server_defaults(data.get('servers', {}).get(server_id))
    if not server or not can_access_server(server, session['email']):
        return jsonify({'error': 'Unauthorized'}), 403

    payload = request.get_json(silent=True) or {}
    cmd = (payload.get('command', '') or '').strip()

    if not cmd:
        return jsonify({'error': 'Komut boş olamaz.'}), 400

    server_path = os.path.join(SERVERS_DIR, server_id)
    log_file_path = os.path.join(server_path, 'server_output.log')

    if is_allowed_npm_command(cmd):
        try:
            run_npm_command(server_path, cmd, log_file_path, server_name=server.get('name', 'my-server'))
            return jsonify({'status': 'success', 'message': 'npm komutu başlatıldı.'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    if server_id in active_processes:
        try:
            proc = active_processes[server_id]['process']
            proc.stdin.write((cmd + '\n').encode('utf-8'))
            proc.stdin.flush()
            return jsonify({'status': 'success', 'message': 'Komut gönderildi.'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    return jsonify({'error': 'Sunucu kapalı. Komut gönderilemez.'}), 400


@app.route('/dashboard/<server_id>', methods=['GET', 'POST'])
def dashboard(server_id):
    if 'username' not in session:
        return redirect(url_for('login'))

    data = load_data()
    current_user = get_current_user_record(data)
    if EMAIL_VERIFICATION_REQUIRED and not is_user_verified(current_user):
        flash('Bu sayfayı kullanmak için hesabını doğrulamalısın.', 'warning')
        return redirect(url_for('login'))

    server = ensure_server_defaults(data.get('servers', {}).get(server_id))

    if not server or not can_access_server(server, session['email']):
        return redirect(url_for('dashboard_menu'))

    server_path = os.path.join(SERVERS_DIR, server_id)
    os.makedirs(server_path, exist_ok=True)

    is_owner = normalize_email(server.get('owner')) == normalize_email(session['email'])

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'start':
            if server_id not in active_processes:
                main_file = server.get('main_file', 'index.js')
                target_file = os.path.join(server_path, main_file)
                log_file_path = os.path.join(server_path, 'server_output.log')

                if not os.path.exists(target_file):
                    flash(f'Başlatma hatası: {main_file} dosyası bulunamadı!', 'danger')
                else:
                    try:
                        log_file = open(log_file_path, 'a', encoding='utf-8')
                        ensure_package_json_exists(server_path, server.get('name', 'my-server'))
                        auto_install_dependencies_if_needed(
                            server_path,
                            log_file,
                            server_name=server.get('name', 'my-server')
                        )

                        process = subprocess.Popen(
                            ['node', main_file],
                            cwd=server_path,
                            stdin=subprocess.PIPE,
                            stdout=log_file,
                            stderr=subprocess.STDOUT
                        )

                        active_processes[server_id] = {
                            "process": process,
                            "log_file": log_file
                        }

                        server['status'] = 'Çalışıyor'
                        flash('Sanal makine başarıyla çalıştırıldı.', 'success')

                    except Exception as e:
                        flash(f'Başlatma başarısız. Hata: {str(e)}', 'danger')
            else:
                flash('Sunucu zaten çalışıyor!', 'warning')

        elif action == 'stop':
            if server_id in active_processes:
                stop_active_server(server_id)
                server['status'] = 'Durduruldu'
                flash('Sanal sunucu işlemi durduruldu.', 'warning')
            else:
                server['status'] = 'Durduruldu'

        elif action == 'save_file':
            filename = request.form.get('filename', '').strip()
            file_content = request.form.get('file_content', '')

            if is_safe_relative_path(filename) and filename:
                safe_path = os.path.join(server_path, filename)
                os.makedirs(os.path.dirname(safe_path), exist_ok=True)
                with open(safe_path, 'w', encoding='utf-8') as f:
                    f.write(file_content)
                flash(f'"{filename}" başarıyla kaydedildi.', 'success')
            else:
                flash('Geçersiz dosya yolu.', 'danger')

        elif action == 'create_folder':
            foldername = request.form.get('foldername', '').strip()

            if is_safe_relative_path(foldername) and foldername:
                os.makedirs(os.path.join(server_path, foldername), exist_ok=True)
                flash(f'"{foldername}" klasörü oluşturuldu.', 'success')
            else:
                flash('Geçersiz klasör yolu.', 'danger')

        elif action == 'delete_file':
            filename = request.form.get('filename', '').strip()

            if is_safe_relative_path(filename) and filename and filename != 'server_output.log':
                safe_path = os.path.join(server_path, filename)
                if os.path.exists(safe_path):
                    if os.path.isdir(safe_path):
                        shutil.rmtree(safe_path)
                    else:
                        os.remove(safe_path)
                    flash(f'"{filename}" başarıyla silindi.', 'success')
            else:
                flash('Bu dosya silinemez.', 'danger')

        elif action == 'update_settings':
            new_name = (request.form.get('server_name') or server.get('name', '')).strip()[:16]
            new_bot_token = request.form.get('bot_token', '')
            new_main_file = (request.form.get('main_file') or server.get('main_file', 'index.js')).strip()

            if not is_safe_relative_path(new_main_file):
                flash('Geçersiz ana dosya yolu.', 'danger')
            else:
                server['name'] = new_name
                server['token'] = new_bot_token
                server['main_file'] = new_main_file
                flash('Ayarlar güncellendi.', 'info')

        elif action == 'add_collaborator':
            if not can_share_server(server, session['email']):
                flash('Bu işlemi sadece sunucu sahibi yapabilir.', 'danger')
            else:
                collaborator_email = normalize_email(request.form.get('collaborator_email'))

                if not collaborator_email:
                    flash('E-posta boş olamaz.', 'danger')
                elif collaborator_email == normalize_email(server.get('owner')):
                    flash('Sahibi zaten ekli.', 'warning')
                else:
                    user_exists = collaborator_email in data.get('users', {})
                    if not user_exists:
                        flash('Bu e-posta kayıtlı değil. Önce o kişi kayıt olmalı.', 'danger')
                    else:
                        server.setdefault('collaborators', [])
                        if collaborator_email not in server['collaborators']:
                            server['collaborators'].append(collaborator_email)
                            flash(f'{collaborator_email} sunucuya eklendi.', 'success')
                        else:
                            flash('Bu kişi zaten ekli.', 'warning')

        data['servers'][server_id] = server
        save_data(data)
        return redirect(url_for('dashboard', server_id=server_id))

    files_list = []
    for root, dirs, files in os.walk(server_path):
        dirs[:] = [d for d in dirs if d != 'node_modules']

        for d in dirs:
            rel_dir = os.path.relpath(os.path.join(root, d), server_path).replace('\\', '/')
            if 'node_modules' not in rel_dir.split('/'):
                files_list.append({"name": rel_dir + "/", "is_dir": True})

        for file in files:
            rel_file = os.path.relpath(os.path.join(root, file), server_path).replace('\\', '/')
            if rel_file != 'server_output.log' and 'node_modules' not in rel_file.split('/'):
                files_list.append({"name": rel_file, "is_dir": False})

    files_list = sorted(files_list, key=lambda x: (not x['is_dir'], x['name']))

    return render_template(
        'dashboard.html',
        server=server,
        files=files_list,
        collaborators=server.get('collaborators', []),
        is_owner=is_owner
    )


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


# ==================== 5. EL İLE BAŞLATMA (LOCAL TEST İÇİN) ====================

if __name__ == '__main__':
    init_db()
    resume_all_running_servers()
    app.run(host='0.0.0.0', port=5000, debug=False)
