import os
import json
import logging
import uuid
import subprocess
import shutil
import shlex
import time
from flask import Flask, render_template, jsonify, request, session, flash, redirect, url_for

try:
    import psutil
except ImportError:
    psutil = None

app = Flask(__name__)
app.secret_key = 'discell_super_secret_safe_key_2026'

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, 'data')
DB_FILE = os.path.join(DATA_DIR, 'db.json')
SERVERS_DIR = os.path.join(BASE_DIR, 'servers_data')

active_processes = {}
MAX_SERVERS_PER_USER = 3


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


def get_user_server_count(data, email):
    return sum(1 for srv in data.get('servers', {}).values() if srv.get('owner') == email)


def is_allowed_npm_command(command: str) -> bool:
    cmd = (command or "").strip()
    allowed_prefixes = (
        "npm install",
        "npm i",
        "npm update",
        "npm uninstall",
        "npm remove",
    )
    return any(cmd == prefix or cmd.startswith(prefix + " ") for prefix in allowed_prefixes)


def is_process_running(proc):
    return proc is not None and proc.poll() is None


def get_active_process(server_id):
    proc_info = active_processes.get(server_id)
    if not proc_info:
        return None
    proc = proc_info.get("process")
    if not is_process_running(proc):
        return None
    return proc


def sync_server_status(server_id, server_data):
    running = get_active_process(server_id) is not None
    new_status = 'Çalışıyor' if running else 'Durduruldu'
    changed = server_data.get('status') != new_status
    server_data['status'] = new_status
    return changed, new_status


def run_npm_command(server_path: str, command: str, log_file_path: str):
    args = shlex.split(command)

    if not args:
        raise ValueError("Komut boş olamaz.")

    if args[0] != "npm":
        raise ValueError("Sadece npm komutları destekleniyor.")

    with open(log_file_path, 'a', encoding='utf-8') as log_file:
        log_file.write(f"\n[Sistem CMD] {command} başlatıldı...\n")
        log_file.flush()

        subprocess.Popen(
            args,
            cwd=server_path,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            shell=(os.name == 'nt')
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


def auto_install_dependencies_if_needed(server_path: str, log_file):
    package_json_path = os.path.join(server_path, 'package.json')

    if os.path.exists(package_json_path):
        log_file.write("\n[Sistem] package.json bulundu. Otomatik npm install yapılıyor...\n")
        log_file.flush()

        result = subprocess.run(
            ["npm", "install"],
            cwd=server_path,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            shell=(os.name == 'nt')
        )

        if result.returncode != 0:
            raise RuntimeError("Otomatik npm install başarısız oldu.")


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
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
        email = request.form.get('email')
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
    user_servers = {k: v for k, v in data.get('servers', {}).items() if v.get('owner') == session['email']}

    changed = False
    for srv_id, srv_data in user_servers.items():
        c, _ = sync_server_status(srv_id, srv_data)
        if c:
            data['servers'][srv_id] = srv_data
            changed = True

    if changed:
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
        "owner": session['email'],
        "name": server_name,
        "token": bot_token,
        "status": "Durduruldu",
        "main_file": "index.js"
    }
    save_data(data)

    server_path = os.path.join(SERVERS_DIR, server_id)
    os.makedirs(server_path, exist_ok=True)
    os.makedirs(os.path.join(server_path, 'data'), exist_ok=True)

    default_code = (
        f"// {server_name} - Ana Dosya\n"
        f"console.log('Bot başlatılıyor...');\n"
    )

    default_package = {
        "name": "bot",
        "version": "1.0.0",
        "main": "index.js",
        "dependencies": {
            "discord.js": "^14.0.0"
        }
    }

    with open(os.path.join(server_path, 'index.js'), 'w', encoding='utf-8') as f:
        f.write(default_code)

    with open(os.path.join(server_path, 'package.json'), 'w', encoding='utf-8') as f:
        json.dump(default_package, f, indent=4)

    flash(f'"{server_name}" isimli gerçek sanal ortam oluşturuldu!', 'success')
    return redirect(url_for('dashboard_menu'))


@app.route('/dashboard/<server_id>/delete', methods=['POST'])
def delete_server(server_id):
    if 'username' not in session:
        return redirect(url_for('login'))

    data = load_data()
    server = data.get('servers', {}).get(server_id)

    if not server or server.get('owner') != session['email']:
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
    server = data.get('servers', {}).get(server_id)
    if not server or server.get('owner') != session['email']:
        return jsonify({"status": "error", "message": "Yetkisiz erişim."}), 403

    filename = request.args.get('file', '').strip()
    safe_path = os.path.join(SERVERS_DIR, server_id, filename)

    if not os.path.exists(safe_path) or os.path.isdir(safe_path) or '..' in filename:
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
    server = data.get('servers', {}).get(server_id)
    if not server or server.get('owner') != session['email']:
        return jsonify({'error': 'Unauthorized'}), 403

    log_path = os.path.join(SERVERS_DIR, server_id, 'server_output.log')
    if os.path.exists(log_path):
        with open(log_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            return jsonify({'logs': "".join(lines[-100:])})

    return jsonify({'logs': 'Henüz log yok veya sunucu kapalı.'})


@app.route('/api/server/<server_id>/stats', methods=['GET'])
def get_stats(server_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 403

    data = load_data()
    server = data.get('servers', {}).get(server_id)
    if not server or server.get('owner') != session['email']:
        return jsonify({'error': 'Unauthorized'}), 403

    if psutil is None:
        return jsonify({
            'status': 'offline',
            'cpu': 0,
            'ram': 0,
            'message': 'psutil yüklü değil'
        })

    proc = get_active_process(server_id)
    if proc is not None:
        try:
            p = psutil.Process(proc.pid)
            total_cpu = p.cpu_percent(interval=0.1)
            total_mem = p.memory_info().rss

            for child in p.children(recursive=True):
                try:
                    total_cpu += child.cpu_percent(interval=0)
                    total_mem += child.memory_info().rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

            ram_mb = total_mem / (1024 * 1024)
            return jsonify({
                'status': 'success',
                'cpu': round(total_cpu, 2),
                'ram': round(ram_mb, 2)
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            stop_active_server(server_id)

    return jsonify({'status': 'offline', 'cpu': 0, 'ram': 0})


@app.route('/api/server/<server_id>/command', methods=['POST'])
def send_command(server_id):
    if 'username' not in session:
        return jsonify({'error': 'Unauthorized'}), 403

    data = load_data()
    server = data.get('servers', {}).get(server_id)
    if not server or server.get('owner') != session['email']:
        return jsonify({'error': 'Unauthorized'}), 403

    payload = request.get_json(silent=True) or {}
    cmd = (payload.get('command', '') or '').strip()

    if not cmd:
        return jsonify({'error': 'Komut boş olamaz.'}), 400

    server_path = os.path.join(SERVERS_DIR, server_id)
    log_file_path = os.path.join(server_path, 'server_output.log')

    if is_allowed_npm_command(cmd):
        try:
            run_npm_command(server_path, cmd, log_file_path)
            return jsonify({'status': 'success', 'message': 'npm komutu başlatıldı.'})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

    proc = get_active_process(server_id)
    if proc is not None:
        try:
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
    server = data.get('servers', {}).get(server_id)

    if not server or server.get('owner') != session['email']:
        return redirect(url_for('dashboard_menu'))

    server_path = os.path.join(SERVERS_DIR, server_id)
    os.makedirs(server_path, exist_ok=True)

    changed, _ = sync_server_status(server_id, server)
    if changed:
        data['servers'][server_id] = server
        save_data(data)

    if request.method == 'POST':
        action = request.form.get('action')

        if action == 'start':
            if get_active_process(server_id) is None:
                main_file = server.get('main_file', 'index.js')
                target_file = os.path.join(server_path, main_file)
                log_file_path = os.path.join(server_path, 'server_output.log')

                if not os.path.exists(target_file):
                    flash(f'Başlatma hatası: {main_file} dosyası bulunamadı!', 'danger')
                else:
                    try:
                        log_file = open(log_file_path, 'a', encoding='utf-8')
                        auto_install_dependencies_if_needed(server_path, log_file)

                        custom_env = os.environ.copy()
                        custom_env["NODE_PATH"] = os.path.join(server_path, 'node_modules')

                        process = subprocess.Popen(
                            ['node', '--max-old-space-size=512', main_file],
                            cwd=server_path,
                            stdin=subprocess.PIPE,
                            stdout=log_file,
                            stderr=subprocess.STDOUT,
                            env=custom_env
                        )

                        active_processes[server_id] = {
                            "process": process,
                            "log_file": log_file
                        }

                        time.sleep(0.5)

                        if process.poll() is not None:
                            stop_active_server(server_id)
                            server['status'] = 'Durduruldu'
                            data['servers'][server_id] = server
                            save_data(data)
                            flash('Bot hemen kapandı. Logları kontrol edin.', 'danger')
                        else:
                            server['status'] = 'Çalışıyor'
                            data['servers'][server_id] = server
                            save_data(data)
                            flash('Sanal makine başarıyla çalıştırıldı. (İzole Ortam & Limit: 512MB RAM)', 'success')

                    except Exception as e:
                        flash(f'Başlatma başarısız. Hata: {str(e)}', 'danger')
            else:
                flash('Sunucu zaten çalışıyor!', 'warning')

        elif action == 'stop':
            if get_active_process(server_id) is not None:
                stop_active_server(server_id)
                server['status'] = 'Durduruldu'
                flash('Sanal sunucu işlemi durduruldu.', 'warning')
            else:
                server['status'] = 'Durduruldu'
                flash('Sunucu zaten kapalıydı.', 'info')

            data['servers'][server_id] = server
            save_data(data)

        elif action == 'save_file':
            filename = request.form.get('filename', '').strip()
            file_content = request.form.get('file_content', '')

            if '..' not in filename and filename:
                safe_path = os.path.join(server_path, filename)
                os.makedirs(os.path.dirname(safe_path), exist_ok=True)
                with open(safe_path, 'w', encoding='utf-8') as f:
                    f.write(file_content)
                flash(f'"{filename}" başarıyla kaydedildi.', 'success')

        elif action == 'create_folder':
            foldername = request.form.get('foldername', '').strip()

            if '..' not in foldername and foldername:
                os.makedirs(os.path.join(server_path, foldername), exist_ok=True)
                flash(f'"{foldername}" klasörü oluşturuldu.', 'success')

        elif action == 'delete_file':
            filename = request.form.get('filename', '').strip()

            if '..' not in filename and filename and filename != 'server_output.log':
                safe_path = os.path.join(server_path, filename)
                if os.path.exists(safe_path):
                    if os.path.isdir(safe_path):
                        shutil.rmtree(safe_path)
                    else:
                        os.remove(safe_path)
                    flash(f'"{filename}" başarıyla silindi.', 'success')

        elif action == 'update_settings':
            server['name'] = request.form.get('server_name')
            server['token'] = request.form.get('bot_token')
            server['main_file'] = request.form.get('main_file')
            flash('Ayarlar güncellendi.', 'info')

        data['servers'][server_id] = server
        save_data(data)
        return redirect(url_for('dashboard', server_id=server_id))

    changed, _ = sync_server_status(server_id, server)
    if changed:
        data['servers'][server_id] = server
        save_data(data)

    files_list = []
    for root, dirs, files in os.walk(server_path):
        for d in dirs:
            rel_dir = os.path.relpath(os.path.join(root, d), server_path).replace('\\', '/')
            if rel_dir != 'node_modules':
                files_list.append({"name": rel_dir + "/", "is_dir": True})
        for file in files:
            rel_file = os.path.relpath(os.path.join(root, file), server_path).replace('\\', '/')
            if rel_file != 'server_output.log' and not rel_file.startswith('node_modules'):
                files_list.append({"name": rel_file, "is_dir": False})

    files_list = sorted(files_list, key=lambda x: (not x['is_dir'], x['name']))

    return render_template('dashboard.html', server=server, files=files_list)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))


if __name__ == '__main__':
    init_db()
    app.run(debug=True, port=5000)
