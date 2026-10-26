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

# ----------------- ESCUDOS DE LOS EQUIPOS -----------------
LOGOS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'assets', 'logos')
LOGO_TEAMS = ('local', 'visitor')
LOGO_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')
LOGO_MAX_BYTES = 5 * 1024 * 1024

def get_logo_urls():
    conn = sqlite3.connect('sports_config.db')
    rows = conn.execute("SELECT team, filename, updated_at FROM team_logos").fetchall()
    conn.close()
    urls = {'local': '', 'visitor': ''}
    for team, filename, updated_at in rows:
        if team in urls and os.path.exists(os.path.join(LOGOS_DIR, filename)):
            urls[team] = f"/logos/{filename}?v={updated_at}"
    return urls

def remove_logo_files(team):
    if os.path.isdir(LOGOS_DIR):
        for f in os.listdir(LOGOS_DIR):
            if os.path.splitext(f)[0] == team:
                os.remove(os.path.join(LOGOS_DIR, f))

def refresh_logos():
    urls = get_logo_urls()
    match_state['localLogo'] = urls['local']
    match_state['visitorLogo'] = urls['visitor']
    ui.send_message('updateScoreboard', match_state)

async def upload_logo(team: str = Form(...), file: UploadFile = File(...)):
    if team not in LOGO_TEAMS:
        return JSONResponse({'error': 'Equipo no válido'}, status_code=400)
    ext = os.path.splitext(file.filename or '')[1].lower()
    if ext not in LOGO_EXTENSIONS:
        return JSONResponse({'error': 'Formato de imagen no permitido'}, status_code=400)
    content = await file.read(LOGO_MAX_BYTES + 1)
    if len(content) > LOGO_MAX_BYTES:
        return JSONResponse({'error': 'La imagen supera los 5 MB'}, status_code=413)

    os.makedirs(LOGOS_DIR, exist_ok=True)
    remove_logo_files(team)
    filename = team + ext
    with open(os.path.join(LOGOS_DIR, filename), 'wb') as f:
        f.write(content)

    conn = sqlite3.connect('sports_config.db')
    conn.execute("INSERT OR REPLACE INTO team_logos (team, filename, updated_at) VALUES (?, ?, ?)",
                 (team, filename, int(time.time())))
    conn.commit()
    conn.close()
    refresh_logos()
    return {'ok': True}

def delete_logo(team: str):
    if team not in LOGO_TEAMS:
        return JSONResponse({'error': 'Equipo no válido'}, status_code=400)
    remove_logo_files(team)
    conn = sqlite3.connect('sports_config.db')
    conn.execute("DELETE FROM team_logos WHERE team = ?", (team,))
    conn.commit()
    conn.close()
    refresh_logos()
    return {'ok': True}

def get_team_names():
    conn = sqlite3.connect('sports_config.db')
    names = dict(conn.execute("SELECT team, name FROM team_names").fetchall())
    conn.close()
    return names

def save_team_names(local_name, visitor_name):
    conn = sqlite3.connect('sports_config.db')
    conn.executemany("INSERT OR REPLACE INTO team_names (team, name) VALUES (?, ?)",
                     [('local', local_name), ('visitor', visitor_name)])
    conn.commit()
    conn.close()

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

saved_names = get_team_names()
match_state['localName'] = saved_names.get('local', match_state['localName'])
match_state['visitorName'] = saved_names.get('visitor', match_state['visitorName'])
match_state['localLogo'] = get_logo_urls()['local']
match_state['visitorLogo'] = get_logo_urls()['visitor']

# ----------------- EVENTOS WEBSOCKET -----------------
def on_connect(client, data):
    # Sincronizar la pantalla al conectarse
    ui.send_message('updateScoreboard', match_state)
    ui.send_message('updateTime', {"seconds": match_state['seconds']})
    ui.send_message('configsList', get_configs()) # Enviamos las configs a la tablet
    
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
    
def setTeamNames(client, data):
    # Si dejan el campo en blanco, volvemos a poner 'LOCAL' o 'VISITANTE' por defecto
    match_state['localName'] = data['localName'] if data['localName'] else 'LOCAL'
    match_state['visitorName'] = data['visitorName'] if data['visitorName'] else 'VISITANTE'
    save_team_names(match_state['localName'], match_state['visitorName'])
    
    ui.send_message('updateScoreboard', match_state)


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
ui.on_message("setTeamNames", setTeamNames)

ui.expose_api("POST", "/upload_logo", upload_logo)
ui.expose_api("POST", "/delete_logo", delete_logo)

# Expose the http_print_message function as an HTTP RESTful API endpoint that can be called from the Web UI using a POST request
# ui.expose_api(method="POST", path="/print_message", function=http_print_message)

App.run(user_loop=cronometro)
