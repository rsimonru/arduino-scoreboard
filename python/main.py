# SPDX-FileCopyrightText: Copyright (C) Arduino s.r.l. and/or its affiliated companies
#
# SPDX-License-Identifier: MPL-2.0

# Example app to demonstrate how to receive requests from the Web UI using the WebUI brick.
# In this example, is defined a WebSocket message handler and an HTTP API endpoint that
# will be called when the user clicks a button in the Web UI and, the Python code will
# log a message in the console.
from arduino.app_utils import *
from arduino.app_utils import Logger
from arduino.app_bricks.web_ui import WebUI
import time
import threading
import sqlite3
import os
from typing import Optional
from fastapi import File, Form, UploadFile
from fastapi.responses import JSONResponse

logger = Logger("ScoreBoard")
ui = WebUI()

# ----------------- BASE DE DATOS SQLITE -----------------
def init_db():
    conn = sqlite3.connect('sports_config.db')
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS configs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, timer_mode TEXT, default_minutes INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS team_names (team TEXT PRIMARY KEY, name TEXT NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS team_logos (team TEXT PRIMARY KEY, filename TEXT NOT NULL, updated_at INTEGER NOT NULL)")
    c.execute("CREATE TABLE IF NOT EXISTS teams (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE COLLATE NOCASE, logo TEXT, updated_at INTEGER NOT NULL DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS selected_teams (side TEXT PRIMARY KEY, team_id INTEGER)")
    c.execute("CREATE TABLE IF NOT EXISTS promo_video (id INTEGER PRIMARY KEY CHECK (id = 1), filename TEXT NOT NULL, updated_at INTEGER NOT NULL)")

    # Migración única de los nombres/escudos antiguos (local/visitante) a la tabla de equipos
    c.execute("SELECT COUNT(*) FROM teams")
    if c.fetchone()[0] == 0:
        names = dict(c.execute("SELECT team, name FROM team_names").fetchall())
        logos = {t: (f, u) for t, f, u in c.execute("SELECT team, filename, updated_at FROM team_logos").fetchall()}
        for side in ('local', 'visitor'):
            name = names.get(side)
            if not name or name in ('LOCAL', 'VISITANTE'):
                continue
            logo, updated = logos.get(side, (None, 0))
            c.execute("INSERT OR IGNORE INTO teams (name, logo, updated_at) VALUES (?, ?, ?)", (name, logo, updated))
            row = c.execute("SELECT id FROM teams WHERE name = ?", (name,)).fetchone()
            c.execute("INSERT OR REPLACE INTO selected_teams (side, team_id) VALUES (?, ?)", (side, row[0]))
    conn.commit()
    
    # Si la base de datos está vacía, insertamos la configuración por defecto
    c.execute("SELECT COUNT(*) FROM configs")
    if c.fetchone()[0] == 0:
        defaults = [
            ('Fútbol Sala', 'down', 20),
            ('Fútbol 11', 'up', 45),
            ('Baloncesto', 'down', 10)
        ]
        c.executemany("INSERT INTO configs (name, timer_mode, default_minutes) VALUES (?, ?, ?)", defaults)
        conn.commit()
    conn.close()

def get_configs():
    conn = sqlite3.connect('sports_config.db')
    c = conn.cursor()
    c.execute("SELECT * FROM configs")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "timer_mode": r[2], "default_minutes": r[3]} for r in rows]

# ----------------- EQUIPOS Y ESCUDOS -----------------
LOGOS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets', 'logos')
LOGO_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')
LOGO_MAX_BYTES = 5 * 1024 * 1024
SIDES = ('local', 'visitor')
DEFAULT_NAMES = {'local': 'LOCAL', 'visitor': 'VISITANTE'}

def logo_url(logo, updated_at):
    if logo and os.path.exists(os.path.join(LOGOS_DIR, logo)):
        return f"/logos/{logo}?v={updated_at}"
    return ''

def get_teams():
    conn = sqlite3.connect('sports_config.db')
    rows = conn.execute("SELECT id, name, logo, updated_at FROM teams ORDER BY name COLLATE NOCASE").fetchall()
    conn.close()
    return [{'id': r[0], 'name': r[1], 'logo': logo_url(r[2], r[3])} for r in rows]

def get_selected_teams():
    conn = sqlite3.connect('sports_config.db')
    rows = conn.execute("SELECT side, team_id FROM selected_teams").fetchall()
    conn.close()
    return {side: team_id for side, team_id in rows}

def apply_selected_teams():
    teams = {t['id']: t for t in get_teams()}
    selected = get_selected_teams()
    for side in SIDES:
        team = teams.get(selected.get(side))
        match_state[side + 'TeamId'] = team['id'] if team else None
        match_state[side + 'Name'] = team['name'] if team else DEFAULT_NAMES[side]
        match_state[side + 'Logo'] = team['logo'] if team else ''

def broadcast_teams():
    apply_selected_teams()
    ui.send_message('teamsList', get_teams())
    ui.send_message('updateScoreboard', match_state)

def remove_logo_file(filename):
    if not filename:
        return
    conn = sqlite3.connect('sports_config.db')
    in_use = conn.execute("SELECT COUNT(*) FROM teams WHERE logo = ?", (filename,)).fetchone()[0]
    conn.close()
    path = os.path.join(LOGOS_DIR, filename)
    if not in_use and os.path.isfile(path):
        os.remove(path)

async def save_team(id: Optional[int] = Form(None), name: str = Form(...),
                    remove_logo: bool = Form(False), file: Optional[UploadFile] = File(None)):
    name = name.strip()
    if not name:
        return JSONResponse({'error': 'El nombre del equipo es obligatorio'}, status_code=400)

    content, ext = None, ''
    if file is not None and file.filename:
        ext = os.path.splitext(file.filename)[1].lower()
        if ext not in LOGO_EXTENSIONS:
            return JSONResponse({'error': 'Formato de imagen no permitido'}, status_code=400)
        content = await file.read(LOGO_MAX_BYTES + 1)
        if len(content) > LOGO_MAX_BYTES:
            return JSONResponse({'error': 'La imagen supera los 5 MB'}, status_code=413)

    conn = sqlite3.connect('sports_config.db')
    old_logo = None
    new_logo = None
    try:
        if id is None:
            cur = conn.execute("INSERT INTO teams (name, logo, updated_at) VALUES (?, NULL, ?)", (name, int(time.time())))
            id = cur.lastrowid
        else:
            row = conn.execute("SELECT logo FROM teams WHERE id = ?", (id,)).fetchone()
            if row is None:
                return JSONResponse({'error': 'Equipo no encontrado'}, status_code=404)
            old_logo = row[0]
            conn.execute("UPDATE teams SET name = ?, updated_at = ? WHERE id = ?", (name, int(time.time()), id))

        new_logo = old_logo
        if content is not None:
            os.makedirs(LOGOS_DIR, exist_ok=True)
            new_logo = f"team_{id}_{int(time.time())}{ext}"
            with open(os.path.join(LOGOS_DIR, new_logo), 'wb') as f:
                f.write(content)
        elif remove_logo:
            new_logo = None
        conn.execute("UPDATE teams SET logo = ? WHERE id = ?", (new_logo, id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return JSONResponse({'error': 'Ya existe un equipo con ese nombre'}, status_code=409)
    finally:
        conn.close()

    if old_logo and old_logo != new_logo:
        remove_logo_file(old_logo)
    broadcast_teams()
    return {'ok': True, 'id': id}

def delete_team(id: int):
    conn = sqlite3.connect('sports_config.db')
    row = conn.execute("SELECT logo FROM teams WHERE id = ?", (id,)).fetchone()
    conn.execute("DELETE FROM teams WHERE id = ?", (id,))
    conn.execute("DELETE FROM selected_teams WHERE team_id = ?", (id,))
    conn.commit()
    conn.close()
    if row:
        remove_logo_file(row[0])
    broadcast_teams()
    return {'ok': True}

def select_team(client, data):
    side = str(data.get('side'))
    if side not in SIDES:
        return
    team_id = data.get('id')
    conn = sqlite3.connect('sports_config.db')
    if team_id in (None, ''):
        conn.execute("DELETE FROM selected_teams WHERE side = ?", (side,))
    else:
        conn.execute("INSERT OR REPLACE INTO selected_teams (side, team_id) VALUES (?, ?)", (side, int(team_id)))
    conn.commit()
    conn.close()
    broadcast_teams()

# ----------------- VÍDEO PROMOCIONAL -----------------
VIDEO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets', 'video')
VIDEO_EXTENSIONS = ('.mp4', '.webm', '.ogg', '.mov', '.m4v')
VIDEO_MAX_BYTES = 500 * 1024 * 1024

def get_promo_url():
    conn = sqlite3.connect('sports_config.db')
    row = conn.execute("SELECT filename, updated_at FROM promo_video WHERE id = 1").fetchone()
    conn.close()
    if row and os.path.exists(os.path.join(VIDEO_DIR, row[0])):
        return f"/video/{row[0]}?v={row[1]}"
    return ''

def remove_video_files(keep=None):
    if os.path.isdir(VIDEO_DIR):
        for f in os.listdir(VIDEO_DIR):
            if f != keep:
                os.remove(os.path.join(VIDEO_DIR, f))

def refresh_promo():
    match_state['promoUrl'] = get_promo_url()
    match_state['promoPosition'] = 0
    if not match_state['promoUrl']:
        match_state['promoPlaying'] = False
    ui.send_message('updateScoreboard', match_state)

async def upload_promo(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or '')[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        return JSONResponse({'error': 'Formato de vídeo no permitido (mp4, webm, ogg, mov, m4v)'}, status_code=400)

    os.makedirs(VIDEO_DIR, exist_ok=True)
    stamp = int(time.time())
    filename = f"promo_{stamp}{ext}"
    path = os.path.join(VIDEO_DIR, filename)
    size = 0
    with open(path, 'wb') as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > VIDEO_MAX_BYTES:
                f.close()
                os.remove(path)
                return JSONResponse({'error': 'El vídeo supera los 500 MB'}, status_code=413)
            f.write(chunk)

    conn = sqlite3.connect('sports_config.db')
    conn.execute("INSERT OR REPLACE INTO promo_video (id, filename, updated_at) VALUES (1, ?, ?)", (filename, stamp))
    conn.commit()
    conn.close()
    remove_video_files(keep=filename)
    match_state['promoPlaying'] = False
    refresh_promo()
    return {'ok': True}

def delete_promo():
    conn = sqlite3.connect('sports_config.db')
    conn.execute("DELETE FROM promo_video")
    conn.commit()
    conn.close()
    remove_video_files()
    refresh_promo()
    return {'ok': True}

def set_promo_playing(client, data):
    playing = bool((data or {}).get('playing'))
    match_state['promoPlaying'] = playing and bool(match_state['promoUrl'])
    ui.send_message('updateScoreboard', match_state)

def set_promo_position(client, data):
    # La posición la reporta la pantalla del marcador al parar el vídeo
    try:
        position = max(0.0, float((data or {}).get('position', 0)))
    except (TypeError, ValueError):
        return
    match_state['promoPosition'] = position

def restart_promo(client, data):
    if not match_state['promoUrl']:
        return
    match_state['promoPosition'] = 0
    match_state['promoRestartId'] += 1
    match_state['promoPlaying'] = True
    ui.send_message('updateScoreboard', match_state)

# Inicializamos la base de datos al arrancar
init_db()

# ----------------- ESTADO DEL PARTIDO -----------------
score_lock = threading.Lock()
match_state = {
    'sport_name': 'Fútbol Sala',
    'timer_mode': 'down',
    'localName': 'LOCAL',        # NUEVO
    'visitorName': 'VISITANTE',  # NUEVO
    'localLogo': '',
    'visitorLogo': '',
    'localTeamId': None,
    'visitorTeamId': None,
    'promoUrl': '',
    'promoPlaying': False,
    'promoPosition': 0,
    'promoRestartId': 0,
    'localPoints': 0,
    'visitorPoints': 0,
    'localFouls': 0,
    'visitorFouls': 0,
    'seconds': 20 * 60,
    'is_running': False
}

# Hilo en segundo plano para manejar el tiempo sin bloquear el servidor
def cronometro():
    while True:
        if match_state['is_running']:
            if str(match_state['timer_mode']) == 'up':
                match_state['seconds'] += 1
            elif str(match_state['timer_mode']) == 'down':
                if int(match_state['seconds']) > 0:
                    match_state['seconds'] -= 1
                else:
                    match_state['is_running'] = False # El reloj se para al llegar a 0
                    ui.send_message('updateScoreboard', match_state)
                    buzzer('main', {'duration': 3000})
                    
            ui.send_message('updateTime', {"seconds": match_state['seconds']})
        time.sleep(1)

apply_selected_teams()
match_state['promoUrl'] = get_promo_url()

# ----------------- EVENTOS WEBSOCKET -----------------
def on_connect(client, data):
    # Sincronizar la pantalla al conectarse
    ui.send_message('updateScoreboard', match_state)
    ui.send_message('updateTime', {"seconds": match_state['seconds']})
    ui.send_message('configsList', get_configs()) # Enviamos las configs a la tablet
    ui.send_message('teamsList', get_teams())
    
def load_config(client, data):
    config_id = int(data['id'])
    configs = get_configs()
    selected = next((c for c in configs if c['id'] == config_id), None)
    
    if selected:
        match_state['sport_name'] = selected['name']
        match_state['timer_mode'] = selected['timer_mode']
        match_state['is_running'] = False
        # Si es cuenta atrás, ponemos el tiempo máximo. Si es hacia arriba, empezamos en 0
        match_state['seconds'] = selected['default_minutes'] * 60 if selected['timer_mode'] == 'down' else 0
        match_state['localPoints'] = 0
        match_state['visitorPoints'] = 0
        match_state['localFouls'] = 0
        match_state['visitorFouls'] = 0
        
        ui.send_message('updateScoreboard', match_state)
        ui.send_message('updateTime', {"seconds": match_state['seconds']})
    
def addPoints(client, data):
    team = str(data['team'])
    points = int(data['points'])
    key = 'localPoints' if team == 'local' else 'visitorPoints'
    with score_lock:
        # Evitar puntuaciones negativas
        match_state[key] = max(0, match_state[key] + points)
            
    ui.send_message('updateScoreboard', match_state)

def addFouls(client, data):
    key = 'localFouls' if str(data['team']) == 'local' else 'visitorFouls'
    with score_lock:
        match_state[key] = max(0, match_state[key] + int(data['fouls']))
    ui.send_message('updateScoreboard', match_state)

def timeToggle(client, data):
    # Invierte el estado (si estaba en False pasa a True, y viceversa)
    match_state['is_running'] = not match_state['is_running']
    # Enviamos el nuevo estado a todos los dispositivos conectados
    ui.send_message('updateScoreboard', match_state)

def buzzer(client, data):
    duration = 1000
    try:
        duration = max(100, min(int((data or {}).get('duration', 1000)), 5000))
    except (TypeError, ValueError):
        pass
    Bridge.notify("buzzer", duration)

def totalReset(client, data):
    match_state['localPoints'] = 0
    match_state['visitorPoints'] = 0
    match_state['localFouls'] = 0
    match_state['visitorFouls'] = 0
    match_state['is_running'] = False
    
    # Reiniciar el reloj según la configuración actual
    configs = get_configs()
    selected = next((c for c in configs if c['name'] == match_state['sport_name']), None)
    if selected:
        match_state['seconds'] = selected['default_minutes'] * 60 if selected['timer_mode'] == 'down' else 0
        
    ui.send_message('updateScoreboard', match_state)
    ui.send_message('updateTime', {"seconds": match_state['seconds']})

def setTime(client, data):
    total_seconds = int(data['seconds'])
    # Evitar que pongan tiempos negativos
    if total_seconds < 0:
        total_seconds = 0
        
    match_state['seconds'] = total_seconds
    # Enviamos el tiempo actualizado inmediatamente a todas las pantallas
    ui.send_message('updateTime', {"seconds": match_state['seconds']})
    



# Register the function to be called when the events are received from the Web UI via websocket.
ui.on_message("connect", on_connect)
ui.on_message("requestInit", on_connect)
ui.on_message("loadConfig", load_config)
ui.on_message("addPoints", addPoints)
ui.on_message("addFouls", addFouls)
ui.on_message("timeToggle", timeToggle)
ui.on_message("buzzer", buzzer)
ui.on_message("totalReset", totalReset)
ui.on_message("setTime", setTime)
ui.on_message("selectTeam", select_team)
ui.on_message("setPromoPlaying", set_promo_playing)
ui.on_message("setPromoPosition", set_promo_position)
ui.on_message("restartPromo", restart_promo)

ui.expose_api("POST", "/save_team", save_team)
ui.expose_api("POST", "/delete_team", delete_team)
ui.expose_api("POST", "/upload_promo", upload_promo)
ui.expose_api("POST", "/delete_promo", delete_promo)

# Expose the http_print_message function as an HTTP RESTful API endpoint that can be called from the Web UI using a POST request
# ui.expose_api(method="POST", path="/print_message", function=http_print_message)

App.run(user_loop=cronometro)
