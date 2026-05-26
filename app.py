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
import urllib.error
from flask import Flask, render_template, jsonify, request, session, flash, redirect, url_for

app = Flask(__name__)
app.secret_key = 'discell_super_secret_safe_key_2026'

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


# ==================== YENİ: OTOMATİK KURTARMA VE CANLI TUTMA ====================

def keep_alive():
    """10 dakikada bir kendi URL'sine istek atarak Render'ın uyutmasını engeller.
       (Dahili urllib kullanılmıştır, requirements.txt gerektirmez)"""
    while True:
        try:
            # Senin Render URL'in
            urllib.request.urlopen("https://vds-virtual-server.onrender.com/") 
            logging.info("[KEEP-ALIVE] Sunucu kendini pingledi, uyku engellendi.")
        except Exception as e:
            logging.error(f"[KEEP-ALIVE] Ping hatası: {e}")
        time.sleep(600)  # 600 saniye = 10 dakika

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

# ==================== VERİTABANI VE YARDIMCI FONKSİYONLAR ====================

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
    
    # GÜVENLİK: Tehlikeli shell komutlarını (pipe, zincirleme vs.) engelle
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
    if "collaborators" not in server or not isinstance(server.get("collaborators"), list):
        server["collaborators"] = []
    if "main_file" not in server:
        server["main_file"] = "index.js"
    return server


# ==================== KULLANICI ARAYÜZÜ (UI) ROTALARI ====================

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = normalize_email(request.form.get('email'))
        password = request.form.get('password')

        data = load_data()

        if email in data['users']:
            flash('Bu e-posta adresi zaten kayıtlı!', 'danger')
            return redirect(url_for('register'))

        data['users'][email] = {"username": username, "password": password}
        save_data(data)

        flash('Hesabınız başarıyla oluşturuldu! Şimdi giriş yapabilirsiniz.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = normalize_email(request.form.get('email'))
        password = request.form.get('password')

        data = load_data()

        if email in data['users'] and data['users'][email]['password'] == password:
            session['email'] = email
            session['username'] = data['users'][email]['username']
            flash('Başarıyla giriş yapıldı.', 'success')
            return redirect(url_for('dashboard_menu'))
        else:
            flash('Hatalı e-posta veya şifre girdiniz!', 'danger')
            return redirect(url_for('login'))

    return render_template('login.html')


@app.route('/dashboard-menu')
def dashboard_menu():
    if 'username' not in session:
        return redirect(url_for('login'))

    data = load_data()
    current_email = normalize_email(session['email'])

    user_servers = {}
    for k, v in data.get('servers', {}).items():
        v = ensure_server_defaults(v)
        if can_access_server(v, current_email):
            v["is_owner"] = normalize_email(v.get("owner")) == current_email
            user_servers[k] = v

    # Menü yüklenirken aktif olmayanları durduruldu olarak işaretle
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

    server_name = request.form.get('server_name')
    bot_token = request.form.get('bot_token', '')

    data = load_data()

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
        "collaborators": []
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


# --- API: AJAX DOSYA OKUMA ---
@app.route('/dashboard/<server_id>/read-file', methods=['GET'])
def read_file(server_id):
    if 'username' not in session:
        return jsonify({"status": "error", "message": "Oturum açmanız gerekiyor."}), 403

    data = load_data()
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


# --- API: CANLI LOG ÇEKME ---
@app.route('/api/server/<server_id>/logs', methods=['GET'])
def get_logs(server_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 403

    data = load_data()
    server = ensure_server_defaults(data.get('servers', {}).get(server_id))
    if not server or not can_access_server(server, session['email']):
        return jsonify({'error': 'Unauthorized'}), 403

    log_path = os.path.join(SERVERS_DIR, server_id, 'server_output.log')
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            return jsonify({'logs': "".join(lines[-100:])})

    return jsonify({'logs': 'Henüz log yok veya sunucu kapalı.'})


# --- API: KONSOLA KOMUT GÖNDERME ---
@app.route('/api/server/<server_id>/command', methods=['POST'])
def send_command(server_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 403

    data = load_data()
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
            server['name'] = request.form.get('server_name')
            server['token'] = request.form.get('bot_token')
            server['main_file'] = request.form.get('main_file')
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


# ==================== UYGULAMA BAŞLATICI ====================

if __name__ == '__main__':
    # 1. Klasör ve Veritabanı yapısını hazırla
    init_db()
    
    # 2. Render yeniden başladıysa, açık kalması gereken botları geri getir
    resume_all_running_servers()
    
    # 3. Sunucuyu uyutmamak için arka planda Ping atıcıyı başlat (urllib ile)
    threading.Thread(target=keep_alive, daemon=True).start()
    
    # 4. Web sunucusunu başlat
    app.run(host='0.0.0.0', port=5000, debug=False)
