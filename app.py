import os
import json
import time
import uuid
import asyncio
import hashlib
import hmac
import sqlite3
from collections import Counter
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()

DB_PATH = os.getenv("DATABASE_PATH", "mafia.db")

NIGHT_SECONDS = int(os.getenv("NIGHT_SECONDS", "45"))
DISCUSSION_SECONDS = int(os.getenv("DISCUSSION_SECONDS", "60"))
VOTE_SECONDS = int(os.getenv("VOTE_SECONDS", "45"))

if not BOT_TOKEN:
    print("WARNING: BOT_TOKEN is not set")

app = FastAPI(title="Mafia Telegram")

os.makedirs("static", exist_ok=True)

app.mount("/static", StaticFiles(directory="static"), name="static")


# ============================================================
# DATABASE
# ============================================================

db_lock = asyncio.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            games INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            kills INTEGER DEFAULT 0,
            deaths INTEGER DEFAULT 0,
            coins INTEGER DEFAULT 100,
            banned INTEGER DEFAULT 0,
            created_at INTEGER
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id TEXT PRIMARY KEY,
            chat_id INTEGER,
            host_id INTEGER,
            status TEXT,
            created_at INTEGER
        )
    """)

    conn.commit()
    conn.close()


init_db()


# ============================================================
# TELEGRAM API
# ============================================================

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


async def tg(method, payload=None):
    if not BOT_TOKEN:
        return None

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{TG_API}/{method}",
                json=payload or {}
            )

            if r.status_code != 200:
                print("Telegram error:", r.text)

            return r.json()
    except Exception as e:
        print("Telegram request error:", e)
        return None


async def send_message(chat_id, text, reply_markup=None):
    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }

    if reply_markup:
        data["reply_markup"] = reply_markup

    return await tg("sendMessage", data)


async def answer_callback(callback_id, text=""):
    return await tg(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_id,
            "text": text
        }
    )


# ============================================================
# TELEGRAM INIT DATA VALIDATION
# ============================================================

def validate_init_data(init_data: str):
    if not init_data or not BOT_TOKEN:
        return None

    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))

        received_hash = parsed.pop("hash", None)

        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{key}={value}"
            for key, value in sorted(parsed.items())
        )

        secret_key = hmac.new(
            b"WebAppData",
            BOT_TOKEN.encode(),
            hashlib.sha256
        ).digest()

        calculated_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        auth_date = int(parsed.get("auth_date", "0"))

        # 24 hours
        if time.time() - auth_date > 86400:
            return None

        user = json.loads(parsed["user"])

        return user

    except Exception:
        return None


# ============================================================
# USER DB
# ============================================================

def save_user(user):
    if not user:
        return

    conn = db()

    conn.execute("""
        INSERT INTO users
        (id, username, first_name, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (
        user["id"],
        user.get("username", ""),
        user.get("first_name", ""),
        int(time.time())
    ))

    conn.commit()
    conn.close()


def get_user(user_id):
    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE id=?",
        (user_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_stats(user_id, win=False, kill=False, death=False):
    conn = db()

    fields = ["games = games + 1"]

    if win:
        fields.append("wins = wins + 1")

    if kill:
        fields.append("kills = kills + 1")

    if death:
        fields.append("deaths = deaths + 1")

    conn.execute(
        f"UPDATE users SET {', '.join(fields)} WHERE id=?",
        (user_id,)
    )

    conn.commit()
    conn.close()


# ============================================================
# ROLES
# ============================================================

ROLES = {
    "don": {
        "name": "Don",
        "team": "mafia",
        "night": "kill"
    },
    "mafioso": {
        "name": "Mafioso",
        "team": "mafia",
        "night": "kill"
    },
    "doctor": {
        "name": "Doctor",
        "team": "town",
        "night": "heal"
    },
    "detective": {
        "name": "Detective",
        "team": "town",
        "night": "investigate"
    },
    "sheriff": {
        "name": "Sheriff",
        "team": "town",
        "night": "investigate"
    },
    "bodyguard": {
        "name": "Bodyguard",
        "team": "town",
        "night": "protect"
    },
    "mayor": {
        "name": "Mayor",
        "team": "town",
        "night": None
    },
    "tracker": {
        "name": "Tracker",
        "team": "town",
        "night": "track"
    },
    "jester": {
        "name": "Jester",
        "team": "neutral",
        "night": None
    },
    "serial_killer": {
        "name": "Serial Killer",
        "team": "neutral",
        "night": "kill"
    }
}


def role_for_index(index, count):
    if count < 5:
        roles = [
            "don",
            "doctor",
            "detective",
            "sheriff",
            "mafioso"
        ]
    elif count < 7:
        roles = [
            "don",
            "mafioso",
            "doctor",
            "detective",
            "sheriff",
            "bodyguard"
        ]
    elif count < 9:
        roles = [
            "don",
            "mafioso",
            "doctor",
            "detective",
            "sheriff",
            "bodyguard",
            "tracker",
            "jester"
        ]
    else:
        roles = [
            "don",
            "mafioso",
            "doctor",
            "detective",
            "sheriff",
            "bodyguard",
            "tracker",
            "mayor",
            "serial_killer",
            "jester"
        ]

    return roles[index % len(roles)]


# ============================================================
# CITY MAP
# ============================================================

CITY_MAP = {
    "square": {
        "name": "Markaziy maydon",
        "x": 50,
        "y": 50
    },
    "police": {
        "name": "Politsiya",
        "x": 25,
        "y": 30
    },
    "hospital": {
        "name": "Kasalxona",
        "x": 75,
        "y": 30
    },
    "bar": {
        "name": "Bar",
        "x": 25,
        "y": 70
    },
    "bank": {
        "name": "Bank",
        "x": 75,
        "y": 70
    },
    "mafia": {
        "name": "Mafia HQ",
        "x": 50,
        "y": 18
    },
    "lab": {
        "name": "Laboratoriya",
        "x": 50,
        "y": 82
    }
}


# ============================================================
# GAME ENGINE
# ============================================================

class Game:
    def __init__(self, game_id, chat_id, host_id):
        self.id = game_id
        self.chat_id = chat_id
        self.host_id = host_id

        self.status = "lobby"
        self.phase = "lobby"

        self.players = {}
        self.votes = {}

        self.created_at = time.time()
        self.phase_started = time.time()
        self.phase_ends = None

        self.day = 0

        self.night_kills = {}
        self.heals = set()
        self.protections = set()
        self.investigations = {}

        self.lock = asyncio.Lock()

    def add_player(self, user):
        uid = int(user["id"])

        if uid in self.players:
            return False

        if len(self.players) >= 20:
            return False

        self.players[uid] = {
            "id": uid,
            "username": user.get("username", ""),
            "name": user.get("first_name", "Player"),
            "role": None,
            "alive": True,
            "location": "square",
            "connected": False
        }

        return True

    def alive_players(self):
        return [
            p for p in self.players.values()
            if p["alive"]
        ]

    def alive_count(self):
        return len(self.alive_players())

    def start(self):
        if len(self.players) < 5:
            return False

        ids = list(self.players.keys())

        # deterministic shuffle-ish using uuid
        import random
        random.shuffle(ids)

        for i, uid in enumerate(ids):
            self.players[uid]["role"] = role_for_index(
                i,
                len(ids)
            )

        self.status = "running"
        self.day = 1
        self.start_night()

        return True

    def start_night(self):
        self.phase = "night"
        self.phase_started = time.time()
        self.phase_ends = time.time() + NIGHT_SECONDS

        self.votes.clear()
        self.night_kills.clear()
        self.heals.clear()
        self.protections.clear()
        self.investigations.clear()

    def start_day(self):
        self.phase = "day"
        self.phase_started = time.time()
        self.phase_ends = time.time() + DISCUSSION_SECONDS
        self.votes.clear()

    def start_vote(self):
        self.phase = "vote"
        self.phase_started = time.time()
        self.phase_ends = time.time() + VOTE_SECONDS
        self.votes.clear()

    def public_state(self, user_id=None):
        players = []

        for p in self.players.values():
            item = {
                "id": p["id"],
                "name": p["name"],
                "username": p["username"],
                "alive": p["alive"],
                "location": p["location"],
                "connected": p["connected"]
            }

            if user_id == p["id"]:
                item["role"] = p["role"]

            players.append(item)

        me = self.players.get(user_id)

        return {
            "game_id": self.id,
            "status": self.status,
            "phase": self.phase,
            "day": self.day,
            "phase_ends": self.phase_ends,
            "players": players,
            "map": CITY_MAP,
            "me": {
                "id": user_id,
                "role": me["role"] if me else None,
                "alive": me["alive"] if me else False,
                "location": me["location"] if me else None
            } if me else None
        }

    def team(self, role):
        return ROLES.get(role, {}).get("team")

    def win_team(self):
        alive = self.alive_players()

        mafia = [
            p for p in alive
            if self.team(p["role"]) == "mafia"
        ]

        town = [
            p for p in alive
            if self.team(p["role"]) == "town"
        ]

        neutral_killers = [
            p for p in alive
            if p["role"] == "serial_killer"
        ]

        if not mafia and not neutral_killers:
            return "town"

        if len(mafia) >= len(town) + len(neutral_killers):
            return "mafia"

        if len(alive) == 1 and neutral_killers:
            return "serial_killer"

        return None

    async def finish_night(self):
        # determine kills
        killed = set()

        # mafia kill
        mafia_targets = list(self.night_kills.items())

        if mafia_targets:
            target, _ = Counter(
                target for target, _ in mafia_targets
            ).most_common(1)[0]

            killed.add(target)

        # serial killer
        for p in self.players.values():
            if (
                p["alive"]
                and p["role"] == "serial_killer"
                and p["id"] in self.night_kills
            ):
                killed.add(p["id"])

        # doctor / bodyguard
        killed -= self.heals
        killed -= self.protections

        for uid in killed:
            if uid in self.players:
                self.players[uid]["alive"] = False
                update_stats(uid, death=True)

        self.day += 1
        self.start_day()

        return killed

    def finish_vote(self):
        if not self.votes:
            self.start_night()
            return None

        counts = Counter(self.votes.values())

        if not counts:
            self.start_night()
            return None

        highest = max(counts.values())
        winners = [
            uid
            for uid, count in counts.items()
            if count == highest
        ]

        if len(winners) != 1:
            self.start_night()
            return None

        target = winners[0]

        if target in self.players:
            self.players[target]["alive"] = False
            update_stats(target, death=True)

        # Jester wins by getting voted
        if target in self.players:
            if self.players[target]["role"] == "jester":
                self.status = "finished"
                self.phase = "finished"
                return target

        self.start_night()

        return target


GAMES = {}
USER_GAME = {}
WS_CONNECTIONS = {}


# ============================================================
# BROADCAST
# ============================================================

async def broadcast_game(game):
    dead = []

    for uid, ws in list(WS_CONNECTIONS.items()):
        if USER_GAME.get(uid) != game.id:
            continue

        try:
            await ws.send_json(
                game.public_state(uid)
            )
        except Exception:
            dead.append(uid)

    for uid in dead:
        WS_CONNECTIONS.pop(uid, None)


# ============================================================
# GAME LOOP
# ============================================================

async def game_loop():
    while True:
        await asyncio.sleep(1)

        for game in list(GAMES.values()):
            if game.status != "running":
                continue

            if not game.phase_ends:
                continue

            if time.time() < game.phase_ends:
                continue

            async with game.lock:

                if game.phase == "night":
                    await game.finish_night()

                elif game.phase == "day":
                    game.start_vote()

                elif game.phase == "vote":
                    game.finish_vote()

                winner = game.win_team()

                if winner:
                    game.status = "finished"
                    game.phase = "finished"

                    for p in game.players.values():
                        update_stats(
                            p["id"],
                            win=(
                                ROLES[p["role"]]["team"] == winner
                                if winner in ["town", "mafia"]
                                else p["role"] == winner
                            )
                        )

                await broadcast_game(game)


# ============================================================
# SUBSCRIPTION
# ============================================================

async def is_subscribed(user_id):
    if not REQUIRED_CHANNEL:
        return True

    result = await tg(
        "getChatMember",
        {
            "chat_id": REQUIRED_CHANNEL,
            "user_id": user_id
        }
    )

    if not result or not result.get("ok"):
        # If Telegram can't check, don't break the whole bot.
        return True

    status = result["result"]["status"]

    return status in {
        "creator",
        "administrator",
        "member"
    }


# ============================================================
# BOT UPDATE PROCESSING
# ============================================================

async def process_update(update):
    try:
        # MESSAGE
        message = update.get("message")

        if message:
            chat = message.get("chat", {})
            user = message.get("from", {})
            text = message.get("text", "")

            if user:
                save_user(user)

            if text.startswith("/start"):
                if chat.get("type") == "private":
                    if not await is_subscribed(user["id"]):
                        await send_message(
                            chat["id"],
                            "❌ Avval kanalga obuna bo‘ling."
                        )
                        return

                    markup = {
                        "inline_keyboard": [[
                            {
                                "text": "🎮 MAFIYA O‘YININI OCHISH",
                                "web_app": {
                                    "url": WEBAPP_URL
                                }
                            }
                        ]]
                    }

                    await send_message(
                        chat["id"],
                        (
                            "<b>🔪 MAFIYA CITY</b>\n\n"
                            "Guruhga kiring, /mafia yozing va "
                            "o‘yinni boshlang."
                        ),
                        markup
                    )

                return

            if text.startswith("/mafia"):
                if chat.get("type") not in {
                    "group",
                    "supergroup"
                }:
                    await send_message(
                        chat["id"],
                        "Bu komandani guruhda ishlating."
                    )
                    return

                game_id = str(uuid.uuid4())[:8]

                game = Game(
                    game_id,
                    chat["id"],
                    user["id"]
                )

                game.add_player(user)

                GAMES[game_id] = game
                USER_GAME[user["id"]] = game_id

                conn = db()
                conn.execute(
                    """
                    INSERT OR REPLACE INTO games
                    (id, chat_id, host_id, status, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        game_id,
                        chat["id"],
                        user["id"],
                        "lobby",
                        int(time.time())
                    )
                )
                conn.commit()
                conn.close()

                markup = {
                    "inline_keyboard": [
                        [{
                            "text": "🧑‍🤝‍🧑 JOIN",
                            "callback_data": f"join:{game_id}"
                        }],
                        [{
                            "text": "🎮 OPEN GAME",
                            "web_app": {
                                "url": WEBAPP_URL
                            }
                        }],
                        [{
                            "text": "▶️ START",
                            "callback_data": f"start:{game_id}"
                        }]
                    ]
                }

                await send_message(
                    chat["id"],
                    (
                        "🔪 <b>MAFIA CITY</b>\n\n"
                        f"Game: <code>{game_id}</code>\n"
                        "Kamida 5 o‘yinchi kerak.\n\n"
                        "🧑‍🤝‍🧑 JOIN tugmasini bosing."
                    ),
                    markup
                )

                return

            if text.startswith("/admin"):
                if user["id"] not in ADMIN_IDS:
                    return

                active = sum(
                    1
                    for g in GAMES.values()
                    if g.status == "running"
                )

                conn = db()
                count = conn.execute(
                    "SELECT COUNT(*) FROM users"
                ).fetchone()[0]
                conn.close()

                await send_message(
                    chat["id"],
                    (
                        "⚙️ <b>ADMIN PANEL</b>\n\n"
                        f"👥 Users: {count}\n"
                        f"🎮 Active games: {active}\n"
                        f"🏠 Games: {len(GAMES)}"
                    )
                )

                return

            if text.startswith("/stats"):
                u = get_user(user["id"])

                if u:
                    await send_message(
                        chat["id"],
                        (
                            "📊 <b>STATISTICS</b>\n\n"
                            f"🎮 Games: {u['games']}\n"
                            f"🏆 Wins: {u['wins']}\n"
                            f"🔪 Kills: {u['kills']}\n"
                            f"💀 Deaths: {u['deaths']}\n"
                            f"🪙 Coins: {u['coins']}"
                        )
                    )

                return

        # CALLBACK
        callback = update.get("callback_query")

        if callback:
            data = callback.get("data", "")
            user = callback.get("from", {})

            save_user(user)

            parts = data.split(":")
            action = parts[0]

            if len(parts) < 2:
                return

            game_id = parts[1]
            game = GAMES.get(game_id)

            await answer_callback(
                callback["id"]
            )

            if not game:
                return

            if action == "join":
                if not await is_subscribed(user["id"]):
                    await answer_callback(
                        callback["id"],
                        "Avval kanalga obuna bo‘ling."
                    )
                    return

                if game.status != "lobby":
                    return

                if game.add_player(user):
                    USER_GAME[user["id"]] = game.id

                    await send_message(
                        game.chat_id,
                        f"👤 {user.get('first_name', 'Player')} qo‘shildi.\n"
                        f"👥 O‘yinchilar: {len(game.players)}/20"
                    )

                    await broadcast_game(game)

                return

            if action == "start":
                if user["id"] != game.host_id:
                    return

                if game.start():
                    conn = db()
                    conn.execute(
                        "UPDATE games SET status='running' WHERE id=?",
                        (game.id,)
                    )
                    conn.commit()
                    conn.close()

                    await send_message(
                        game.chat_id,
                        (
                            "🌙 <b>TUN BOSHLANDI</b>\n\n"
                            "Barcha o‘yinchilar Mini App'ni ochsin."
                        )
                    )

                    await broadcast_game(game)

                return

    except Exception as e:
        print("process_update error:", repr(e))


# ============================================================
# BOT POLLING
# ============================================================

async def bot_polling():
    if not BOT_TOKEN:
        return

    offset = 0

    while True:
        try:
            result = await tg(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": [
                        "message",
                        "callback_query"
                    ]
                }
            )

            if result and result.get("ok"):
                for update in result.get("result", []):
                    offset = update["update_id"] + 1
                    await process_update(update)

        except Exception as e:
            print("Polling:", e)
            await asyncio.sleep(3)


# ============================================================
# MINI APP
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html>
    <head>
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Mafia City</title>
    </head>
    <body style="background:#080808;color:white;font-family:Arial;text-align:center;padding:50px">
        <h1>🔪 MAFIA CITY</h1>
        <p>Server ishlayapti.</p>
        <p>Telegram bot orqali Mini App'ni oching.</p>
    </body>
    </html>
    """


@app.get("/app", response_class=HTMLResponse)
async def mini_app():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()


@app.get("/health")
async def health():
    return {
        "ok": True,
        "games": len(GAMES),
        "users_online": len(WS_CONNECTIONS)
    }


@app.post("/api/auth")
async def api_auth(request: Request):
    body = await request.json()

    init_data = body.get("initData", "")

    user = validate_init_data(init_data)

    if not user:
        return JSONResponse(
            {
                "ok": False,
                "error": "invalid_init_data"
            },
            status_code=401
        )

    save_user(user)

    return {
        "ok": True,
        "user": get_user(user["id"])
    }


# ============================================================
# WEBSOCKET
# ============================================================

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()

    user_id = None

    try:
        auth_message = await ws.receive_json()

        init_data = auth_message.get("initData", "")

        user = validate_init_data(init_data)

        if not user:
            await ws.send_json({
                "type": "error",
                "message": "Telegram authentication failed"
            })
            await ws.close()
            return

        user_id = int(user["id"])

        save_user(user)

        game_id = USER_GAME.get(user_id)

        if not game_id:
            await ws.send_json({
                "type": "waiting",
                "message": "Siz hali o‘yinga qo‘shilmagansiz."
            })
            return

        game = GAMES.get(game_id)

        if not game:
            await ws.send_json({
                "type": "waiting",
                "message": "Aktiv o‘yin topilmadi."
            })
            return

        WS_CONNECTIONS[user_id] = ws

        game.players[user_id]["connected"] = True

        await broadcast_game(game)

        while True:
            data = await ws.receive_json()

            action = data.get("action")

            if action == "move":
                location = data.get("location")

                if location in CITY_MAP:
                    game.players[user_id]["location"] = location

            elif action == "night_action":
                target = data.get("target")

                if (
                    game.phase == "night"
                    and target in game.players
                    and game.players[user_id]["alive"]
                ):
                    me = game.players[user_id]
                    role = me["role"]

                    if role in ["don", "mafioso"]:
                        if game.players[target]["alive"]:
                            game.night_kills[target] = user_id

                    elif role == "serial_killer":
                        game.night_kills[target] = user_id

                    elif role == "doctor":
                        game.heals.add(target)

                    elif role == "bodyguard":
                        game.protections.add(target)

                    elif role in ["detective", "sheriff"]:
                        target_player = game.players[target]

                        game.investigations[user_id] = {
                            "target": target,
                            "team": game.team(
                                target_player["role"]
                            )
                        }

                    elif role == "tracker":
                        target_player = game.players[target]

                        game.investigations[user_id] = {
                            "target": target,
                            "location": target_player["location"]
                        }

            elif action == "vote":
                target = data.get("target")

                if (
                    game.phase == "vote"
                    and game.players[user_id]["alive"]
                    and target in game.players
                    and game.players[target]["alive"]
                ):
                    game.votes[user_id] = target

            elif action == "get_investigation":
                result = game.investigations.get(user_id)

                await ws.send_json({
                    "type": "investigation",
                    "data": result
                })

            await broadcast_game(game)

    except WebSocketDisconnect:
        pass

    except Exception as e:
        print("WebSocket:", repr(e))

    finally:
        if user_id is not None:
            WS_CONNECTIONS.pop(user_id, None)

            game_id = USER_GAME.get(user_id)

            if game_id in GAMES:
                game = GAMES[game_id]

                if user_id in game.players:
                    game.players[user_id]["connected"] = False


# ============================================================
# STARTUP
# ============================================================

@app.on_event("startup")
async def startup():
    asyncio.create_task(bot_polling())
    asyncio.create_task(game_loop())

    print("================================")
    print("MAFIA CITY SERVER STARTED")
    print("================================")
