import os
import threading
import asyncio
from datetime import datetime, timezone

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

import discord
from discord.ext import commands

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None


# ==========================================================
# APP SETUP
# ==========================================================
app = Flask(__name__)
CORS(app)


# ==========================================================
# SAFE ENV HELPERS
# ==========================================================
def env_int(name: str, default: int = 0) -> int:
    """
    Safely read an integer from Railway Variables.
    If the value is missing, blank, or not a number, use the default.
    This prevents Railway from crashing over one bad variable.
    """
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default

    try:
        return int(value)
    except ValueError:
        print(f"WARNING: {name} must be a number. Got {value!r}. Using {default}.")
        return default


DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")

GUILD_ID = env_int("GUILD_ID", 1102370626796261498)
OWNER_ID = env_int("OWNER_ID", 779805628804890644)

TEAM_MEMBER_ROLE_ID = env_int("TEAM_MEMBER_ROLE_ID", 1218334966341959750)
CAPTAIN_ROLE_ID = env_int("CAPTAIN_ROLE_ID", 0)
SEASON_MANAGER_ROLE_ID = env_int("SEASON_MANAGER_ROLE_ID", 1102378245216813127)

TRANSACTIONS_CHANNEL_ID = env_int("TRANSACTIONS_CHANNEL_ID", 0)
NEW_TEAMS_CHANNEL_ID = env_int("NEW_TEAMS_CHANNEL_ID", 0)


# ==========================================================
# MEMORY FALLBACK
# This only gets used if DATABASE_URL is missing.
# It is not permanent storage.
# ==========================================================
memory_teams = {}
memory_scrims = []
memory_season = None
memory_suspensions = {
    "players": {},
    "teams": {}
}


# ==========================================================
# DATABASE HELPERS
# ==========================================================
def database_enabled() -> bool:
    return bool(DATABASE_URL and psycopg2)


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing. Add PostgreSQL to Railway and connect it to this service.")

    if psycopg2 is None:
        raise RuntimeError("psycopg2 is missing. Add psycopg2-binary to requirements.txt.")

    return psycopg2.connect(DATABASE_URL)


def init_db():
    """
    Creates tables if PostgreSQL is connected.
    If DATABASE_URL is missing, the app will still run using memory fallback.
    """
    if not database_enabled():
        print("DATABASE_URL is missing or psycopg2 is not installed. Running with temporary memory storage.")
        return

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            name TEXT PRIMARY KEY,
            abbreviation TEXT NOT NULL,
            captain BIGINT NOT NULL,
            co_captain BIGINT,
            official BOOLEAN DEFAULT FALSE,
            logo_url TEXT,
            team_discord TEXT,
            captain_username TEXT,
            created_at TEXT
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS team_members (
            team_name TEXT REFERENCES teams(name) ON DELETE CASCADE,
            user_id BIGINT NOT NULL,
            PRIMARY KEY (team_name, user_id)
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS seasons (
            name TEXT PRIMARY KEY,
            status TEXT DEFAULT 'creating',
            created_by BIGINT,
            created_at TEXT,
            started_by BIGINT,
            started_at TEXT,
            ended_by BIGINT,
            ended_at TEXT,
            end_reason TEXT,
            is_current BOOLEAN DEFAULT FALSE
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS season_teams (
            season_name TEXT REFERENCES seasons(name) ON DELETE CASCADE,
            team_name TEXT REFERENCES teams(name) ON DELETE CASCADE,
            PRIMARY KEY (season_name, team_name)
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS season_kicked_teams (
            id SERIAL PRIMARY KEY,
            season_name TEXT REFERENCES seasons(name) ON DELETE CASCADE,
            team_name TEXT,
            reason TEXT,
            kicked_by BIGINT,
            kicked_at TEXT
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS suspensions (
            id SERIAL PRIMARY KEY,
            suspension_type TEXT NOT NULL,
            target TEXT NOT NULL,
            reason TEXT,
            seasons INTEGER,
            duration TEXT,
            suspended_by BIGINT,
            suspended_at TEXT,
            active BOOLEAN DEFAULT TRUE
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS scrims (
            id SERIAL PRIMARY KEY,
            team_a TEXT,
            team_b TEXT,
            opponent TEXT,
            date TEXT,
            time TEXT,
            notes TEXT,
            created_by BIGINT,
            status TEXT DEFAULT 'open'
        );
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("Database tables are ready.")


def db_get_teams() -> dict:
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM teams ORDER BY name;")
    team_rows = cur.fetchall()

    cur.execute("SELECT team_name, user_id FROM team_members ORDER BY team_name;")
    member_rows = cur.fetchall()

    cur.close()
    conn.close()

    teams = {}

    for row in team_rows:
        teams[row["name"]] = {
            "abbreviation": row["abbreviation"],
            "captain": row["captain"],
            "co_captain": row["co_captain"],
            "members": [],
            "official": row["official"],
            "logo_url": row["logo_url"],
            "team_discord": row["team_discord"],
            "captain_username": row["captain_username"],
            "created_at": row["created_at"]
        }

    for row in member_rows:
        team_name = row["team_name"]
        if team_name in teams:
            teams[team_name]["members"].append(row["user_id"])

    return teams


def db_find_user_team(user_id: int):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT team_name
        FROM team_members
        WHERE user_id = %s
        LIMIT 1;
    """, (int(user_id),))

    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        return None, None

    teams = db_get_teams()
    team_name = row["team_name"]
    return team_name, teams.get(team_name)


def db_create_team(name, abbreviation, captain_id, captain_username, logo_url=None, team_discord=None):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT name FROM teams WHERE name = %s;", (name,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return {"error": "That team already exists."}

    cur.execute("SELECT team_name FROM team_members WHERE user_id = %s;", (captain_id,))
    if cur.fetchone():
        cur.close()
        conn.close()
        return {"error": "You are already on a team."}

    created_at = datetime.now(timezone.utc).isoformat()

    cur.execute("""
        INSERT INTO teams (
            name,
            abbreviation,
            captain,
            co_captain,
            official,
            logo_url,
            team_discord,
            captain_username,
            created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);
    """, (
        name,
        abbreviation,
        captain_id,
        None,
        False,
        logo_url,
        team_discord,
        captain_username,
        created_at
    ))

    cur.execute("""
        INSERT INTO team_members (team_name, user_id)
        VALUES (%s, %s);
    """, (name, captain_id))

    conn.commit()
    cur.close()
    conn.close()

    return {"success": True, "message": f"Team {name} created."}


def db_set_team_official(team_name: str):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("UPDATE teams SET official = TRUE WHERE name = %s;", (team_name,))

    if cur.rowcount == 0:
        cur.close()
        conn.close()
        return {"error": "Team not found."}

    conn.commit()
    cur.close()
    conn.close()

    return {"success": True, "message": f"{team_name} is now official."}


def db_disband_team(team_name: str):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("DELETE FROM teams WHERE name = %s;", (team_name,))

    if cur.rowcount == 0:
        cur.close()
        conn.close()
        return {"error": "Team not found."}

    conn.commit()
    cur.close()
    conn.close()

    return {"success": True, "message": f"{team_name} disbanded."}


def db_leave_team(team_name: str, user_id: int):
    teams = db_get_teams()

    if team_name not in teams:
        return {"error": "Team not found."}

    team = teams[team_name]

    if int(user_id) == int(team["captain"]):
        return {"error": "Captain cannot leave. Disband the team or transfer captain first."}

    if int(user_id) not in [int(x) for x in team["members"]]:
        return {"error": "You are not on this team."}

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        DELETE FROM team_members
        WHERE team_name = %s AND user_id = %s;
    """, (team_name, user_id))

    if team.get("co_captain") == int(user_id):
        cur.execute("""
            UPDATE teams
            SET co_captain = NULL
            WHERE name = %s;
        """, (team_name,))

    conn.commit()
    cur.close()
    conn.close()

    return {"success": True, "message": "You left the team."}


def db_edit_team(old_name, new_name=None, abbreviation=None, logo_url=None, team_discord=None):
    teams = db_get_teams()

    if old_name not in teams:
        return {"error": "Team not found."}

    final_name = new_name.strip() if new_name else old_name

    if final_name != old_name and final_name in teams:
        return {"error": "A team with that name already exists."}

    conn = get_db_connection()
    cur = conn.cursor()

    if final_name != old_name:
        cur.execute("""
            UPDATE teams
            SET name = %s
            WHERE name = %s;
        """, (final_name, old_name))

    if abbreviation:
        cur.execute("""
            UPDATE teams
            SET abbreviation = %s
            WHERE name = %s;
        """, (abbreviation.upper(), final_name))

    if logo_url is not None:
        cur.execute("""
            UPDATE teams
            SET logo_url = %s
            WHERE name = %s;
        """, (logo_url, final_name))

    if team_discord is not None:
        cur.execute("""
            UPDATE teams
            SET team_discord = %s
            WHERE name = %s;
        """, (team_discord, final_name))

    conn.commit()
    cur.close()
    conn.close()

    return {"success": True, "message": "Team updated."}


def db_get_scrims() -> list:
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM scrims ORDER BY id DESC;")
    rows = cur.fetchall()

    cur.close()
    conn.close()

    scrims = []

    for row in rows:
        scrims.append({
            "id": str(row["id"]),
            "team_a": row["team_a"],
            "team_b": row["team_b"],
            "opponent": row["opponent"],
            "date": row["date"],
            "time": row["time"],
            "notes": row["notes"],
            "created_by": row["created_by"],
            "status": row["status"]
        })

    return scrims


def db_create_scrim(team_a, opponent, date, time, notes, created_by):
    status = "scheduled" if opponent else "open"
    team_b = opponent if opponent else None

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        INSERT INTO scrims (
            team_a,
            team_b,
            opponent,
            date,
            time,
            notes,
            created_by,
            status
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *;
    """, (team_a, team_b, opponent, date, time, notes, created_by, status))

    row = cur.fetchone()
    conn.commit()

    cur.close()
    conn.close()

    return {
        "id": str(row["id"]),
        "team_a": row["team_a"],
        "team_b": row["team_b"],
        "opponent": row["opponent"],
        "date": row["date"],
        "time": row["time"],
        "notes": row["notes"],
        "created_by": row["created_by"],
        "status": row["status"]
    }


def db_accept_scrim(scrim_id, team_b):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM scrims WHERE id = %s;", (int(scrim_id),))
    scrim = cur.fetchone()

    if not scrim:
        cur.close()
        conn.close()
        return {"error": "Scrim not found."}

    if scrim["status"] != "open":
        cur.close()
        conn.close()
        return {"error": "This scrim is not open."}

    if scrim["team_a"] == team_b:
        cur.close()
        conn.close()
        return {"error": "You cannot accept your own scrim."}

    cur.execute("""
        UPDATE scrims
        SET team_b = %s,
            opponent = %s,
            status = 'scheduled'
        WHERE id = %s
        RETURNING *;
    """, (team_b, team_b, int(scrim_id)))

    row = cur.fetchone()
    conn.commit()

    cur.close()
    conn.close()

    return {
        "success": True,
        "scrim": {
            "id": str(row["id"]),
            "team_a": row["team_a"],
            "team_b": row["team_b"],
            "opponent": row["opponent"],
            "date": row["date"],
            "time": row["time"],
            "notes": row["notes"],
            "created_by": row["created_by"],
            "status": row["status"]
        }
    }


def db_get_current_season():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT * FROM seasons WHERE is_current = TRUE LIMIT 1;")
    season = cur.fetchone()

    if not season:
        cur.close()
        conn.close()
        return {}

    cur.execute("""
        SELECT team_name
        FROM season_teams
        WHERE season_name = %s
        ORDER BY team_name;
    """, (season["name"],))

    team_rows = cur.fetchall()

    cur.close()
    conn.close()

    return {
        "name": season["name"],
        "status": season["status"],
        "teams": [row["team_name"] for row in team_rows],
        "created_by": season["created_by"],
        "created_at": season["created_at"],
        "started_by": season["started_by"],
        "started_at": season["started_at"],
        "ended_by": season["ended_by"],
        "ended_at": season["ended_at"],
        "end_reason": season["end_reason"]
    }


def db_get_suspensions():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT *
        FROM suspensions
        WHERE active = TRUE;
    """)

    rows = cur.fetchall()

    cur.close()
    conn.close()

    data = {
        "players": {},
        "teams": {}
    }

    for row in rows:
        entry = {
            "reason": row["reason"],
            "seasons": row["seasons"],
            "duration": row["duration"],
            "suspended_by": row["suspended_by"],
            "suspended_at": row["suspended_at"]
        }

        if row["suspension_type"] == "player":
            data["players"][str(row["target"])] = entry
        elif row["suspension_type"] == "team":
            data["teams"][row["target"]] = entry

    return data


def db_unsuspend(suspension_type, target):
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("""
        UPDATE suspensions
        SET active = FALSE
        WHERE suspension_type = %s
          AND target = %s
          AND active = TRUE;
    """, (suspension_type, str(target)))

    conn.commit()
    cur.close()
    conn.close()

    return {"success": True, "message": "Suspension lifted."}


# ==========================================================
# DISCORD BOT SETUP
# ==========================================================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Discord bot logged in as {bot.user}")


def run_bot_task(coro):
    """
    Safely sends an async Discord task to the bot event loop.
    """
    try:
        loop = bot.loop
        if loop and loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, loop)
        else:
            print("Bot loop is not running yet.")
    except Exception as e:
        print(f"Could not schedule bot task: {e}")


def get_guild():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("Guild not found. Check GUILD_ID and make sure the bot is in the server.")
    return guild


def is_owner(user_id):
    try:
        return int(user_id) == OWNER_ID
    except Exception:
        return False


def get_all_teams():
    if database_enabled():
        return db_get_teams()
    return memory_teams


def find_user_team(user_id):
    if database_enabled():
        return db_find_user_team(user_id)

    user_id = int(user_id)

    for team_name, team in memory_teams.items():
        if user_id in team["members"]:
            return team_name, team

    return None, None


def user_is_captain_or_cocaptain(user_id, team):
    user_id = int(user_id)
    return user_id == team.get("captain") or user_id == team.get("co_captain")


async def assign_team_roles(user_id):
    guild = get_guild()
    if not guild:
        return

    member = guild.get_member(int(user_id))

    if not member:
        try:
            member = await guild.fetch_member(int(user_id))
        except Exception as e:
            print(f"Member not found: {e}")
            return

    roles_to_add = []

    team_role = guild.get_role(TEAM_MEMBER_ROLE_ID)
    if team_role:
        roles_to_add.append(team_role)
    else:
        print("Team Member role not found. Check TEAM_MEMBER_ROLE_ID.")

    if CAPTAIN_ROLE_ID != 0:
        captain_role = guild.get_role(CAPTAIN_ROLE_ID)
        if captain_role:
            roles_to_add.append(captain_role)
        else:
            print("Captain role not found. Check CAPTAIN_ROLE_ID.")

    if roles_to_add:
        try:
            await member.add_roles(*roles_to_add, reason="GTL team created")
            print(f"Roles added to {member}")
        except Exception as e:
            print(f"Could not add roles: {e}")


async def dm_invite(target_id, team_name):
    try:
        user = await bot.fetch_user(int(target_id))
        await user.send(f"You have been invited to join **{team_name}** in GTL.")
        print(f"Invite sent to {target_id}")
    except Exception as e:
        print(f"Could not DM user: {e}")


async def send_channel_message(channel_id, message):
    try:
        channel = bot.get_channel(int(channel_id))
        if channel:
            await channel.send(message)
        else:
            print("Announcement channel not found.")
    except Exception as e:
        print(f"Could not send announcement: {e}")


# ==========================================================
# WEBSITE ROUTES
# ==========================================================
@app.route("/")
def home():
    return send_from_directory(".", "index.html")


@app.route("/api/status")
def api_status():
    return jsonify({
        "status": "GTL API is running",
        "database_enabled": database_enabled(),
        "message": "The backend is alive. Somehow."
    })


# ==========================================================
# BASIC API ROUTES
# ==========================================================
@app.route("/teams", methods=["GET"])
def get_teams():
    return jsonify(get_all_teams())


@app.route("/scrims", methods=["GET"])
def get_scrims():
    if database_enabled():
        return jsonify(db_get_scrims())
    return jsonify(memory_scrims)


@app.route("/suspensions", methods=["GET"])
def get_suspensions():
    if database_enabled():
        return jsonify(db_get_suspensions())
    return jsonify(memory_suspensions)


@app.route("/season/current", methods=["GET"])
def get_current_season():
    if database_enabled():
        return jsonify(db_get_current_season())
    return jsonify(memory_season or {})


# ==========================================================
# TEAM ROUTES
# ==========================================================
@app.route("/teams/create", methods=["POST"])
def create_team():
    data = request.json or {}

    name = data.get("name", "").strip()
    abbreviation = data.get("abbreviation", "").strip().upper()
    captain_id = data.get("captain_id")
    captain_username = data.get("captain_username", "")
    logo_url = data.get("logo_url")
    team_discord = data.get("team_discord")

    if not name or not abbreviation:
        return jsonify({"error": "Team name and abbreviation are required."}), 400

    if not captain_id:
        return jsonify({"error": "Missing captain ID."}), 400

    captain_id = int(captain_id)

    if database_enabled():
        result = db_create_team(
            name=name,
            abbreviation=abbreviation,
            captain_id=captain_id,
            captain_username=captain_username,
            logo_url=logo_url,
            team_discord=team_discord
        )

        if result.get("error"):
            return jsonify(result), 400

        run_bot_task(assign_team_roles(captain_id))
        return jsonify(result)

    if name in memory_teams:
        return jsonify({"error": "That team already exists."}), 400

    existing_team_name, _ = find_user_team(captain_id)
    if existing_team_name:
        return jsonify({"error": "You are already on a team."}), 400

    memory_teams[name] = {
        "abbreviation": abbreviation,
        "captain": captain_id,
        "co_captain": None,
        "members": [captain_id],
        "official": False,
        "logo_url": logo_url,
        "team_discord": team_discord,
        "captain_username": captain_username,
        "created_at": datetime.now(timezone.utc).isoformat()
    }

    run_bot_task(assign_team_roles(captain_id))

    return jsonify({
        "success": True,
        "message": f"Team {name} created."
    })


@app.route("/teams/official", methods=["POST"])
def make_team_official():
    data = request.json or {}

    team_name = data.get("team_name")
    user_id = data.get("user_id")

    if not is_owner(user_id):
        return jsonify({"error": "Only the owner can make teams official."}), 403

    if database_enabled():
        result = db_set_team_official(team_name)
        if result.get("error"):
            return jsonify(result), 404
        return jsonify(result)

    if team_name not in memory_teams:
        return jsonify({"error": "Team not found."}), 404

    memory_teams[team_name]["official"] = True

    return jsonify({
        "success": True,
        "message": f"{team_name} is now official."
    })


@app.route("/teams/disband", methods=["POST"])
def disband_team():
    data = request.json or {}

    team_name = data.get("team_name")
    user_id = data.get("user_id")

    if not team_name or user_id is None:
        return jsonify({"error": "Missing team name or user ID."}), 400

    user_id = int(user_id)
    teams = get_all_teams()

    if team_name not in teams:
        return jsonify({"error": "Team not found."}), 404

    team = teams[team_name]

    if user_id != team["captain"] and user_id != OWNER_ID:
        return jsonify({"error": "Only the captain or owner can disband this team."}), 403

    if database_enabled():
        result = db_disband_team(team_name)
        if result.get("error"):
            return jsonify(result), 404
        return jsonify(result)

    del memory_teams[team_name]

    return jsonify({
        "success": True,
        "message": f"{team_name} disbanded."
    })


@app.route("/teams/leave", methods=["POST"])
def leave_team():
    data = request.json or {}

    user_id = data.get("user_id")
    team_name = data.get("team_name")

    if not user_id or not team_name:
        return jsonify({"error": "Missing user ID or team name."}), 400

    user_id = int(user_id)

    if database_enabled():
        result = db_leave_team(team_name, user_id)
        if result.get("error"):
            return jsonify(result), 400
        return jsonify(result)

    if team_name not in memory_teams:
        return jsonify({"error": "Team not found."}), 404

    team = memory_teams[team_name]

    if user_id == team["captain"]:
        return jsonify({"error": "Captain cannot leave. Disband the team or transfer captain first."}), 400

    if user_id not in team["members"]:
        return jsonify({"error": "You are not on this team."}), 400

    team["members"].remove(user_id)

    if team.get("co_captain") == user_id:
        team["co_captain"] = None

    return jsonify({
        "success": True,
        "message": "You left the team."
    })


@app.route("/teams/invite", methods=["POST"])
def invite_player():
    data = request.json or {}

    team_name = data.get("team_name")
    target_id = data.get("target_id")
    inviter_id = data.get("inviter_id")

    if not team_name or not target_id or not inviter_id:
        return jsonify({"error": "Missing team name, target ID, or inviter ID."}), 400

    target_id = int(target_id)
    inviter_id = int(inviter_id)

    teams = get_all_teams()

    if team_name not in teams:
        return jsonify({"error": "Team not found."}), 404

    team = teams[team_name]

    if not user_is_captain_or_cocaptain(inviter_id, team):
        return jsonify({"error": "Only captains or co-captains can invite players."}), 403

    if len(team["members"]) >= 25:
        return jsonify({"error": "Team roster is full."}), 400

    run_bot_task(dm_invite(target_id, team_name))

    return jsonify({
        "success": True,
        "message": "Invite sent."
    })


@app.route("/teams/edit", methods=["POST"])
def edit_team():
    data = request.json or {}

    old_name = data.get("old_name")
    new_name = data.get("new_name")
    abbreviation = data.get("abbreviation")
    logo_url = data.get("logo_url")
    team_discord = data.get("team_discord")
    user_id = data.get("user_id")

    if not old_name or not user_id:
        return jsonify({"error": "Missing team name or user ID."}), 400

    user_id = int(user_id)
    teams = get_all_teams()

    if old_name not in teams:
        return jsonify({"error": "Team not found."}), 404

    team = teams[old_name]

    if not user_is_captain_or_cocaptain(user_id, team) and user_id != OWNER_ID:
        return jsonify({"error": "Only captains, co-captains, or the owner can edit this team."}), 403

    if database_enabled():
        result = db_edit_team(
            old_name=old_name,
            new_name=new_name,
            abbreviation=abbreviation,
            logo_url=logo_url,
            team_discord=team_discord
        )

        if result.get("error"):
            return jsonify(result), 400

        return jsonify(result)

    final_name = new_name.strip() if new_name else old_name

    if final_name != old_name and final_name in memory_teams:
        return jsonify({"error": "A team with that name already exists."}), 400

    if abbreviation:
        team["abbreviation"] = abbreviation.strip().upper()

    if logo_url is not None:
        team["logo_url"] = logo_url

    if team_discord is not None:
        team["team_discord"] = team_discord

    if final_name != old_name:
        memory_teams[final_name] = team
        del memory_teams[old_name]

    return jsonify({
        "success": True,
        "message": "Team updated."
    })


# ==========================================================
# SCRIM ROUTES
# ==========================================================
@app.route("/scrims/create", methods=["POST"])
def create_scrim():
    data = request.json or {}

    team_a = data.get("team_a")
    opponent = data.get("opponent")
    date = data.get("date")
    time = data.get("time")
    notes = data.get("notes")
    created_by = data.get("created_by")

    if not team_a or not date or not time:
        return jsonify({"error": "Team, date, and time are required."}), 400

    if database_enabled():
        scrim = db_create_scrim(team_a, opponent, date, time, notes, created_by)
        return jsonify({
            "success": True,
            "scrim": scrim
        })

    new_scrim = {
        "id": str(len(memory_scrims) + 1),
        "team_a": team_a,
        "team_b": opponent if opponent else None,
        "opponent": opponent if opponent else None,
        "date": date,
        "time": time,
        "notes": notes,
        "created_by": created_by,
        "status": "scheduled" if opponent else "open"
    }

    memory_scrims.append(new_scrim)

    return jsonify({
        "success": True,
        "scrim": new_scrim
    })


@app.route("/scrims/accept", methods=["POST"])
def accept_scrim():
    data = request.json or {}

    scrim_id = data.get("scrim_id")
    team_b = data.get("team_b")

    if not scrim_id or not team_b:
        return jsonify({"error": "Missing scrim ID or team name."}), 400

    if database_enabled():
        result = db_accept_scrim(scrim_id, team_b)
        if result.get("error"):
            return jsonify(result), 400
        return jsonify(result)

    for scrim in memory_scrims:
        if scrim["id"] == scrim_id:
            if scrim["status"] != "open":
                return jsonify({"error": "This scrim is not open."}), 400

            if scrim["team_a"] == team_b:
                return jsonify({"error": "You cannot accept your own scrim."}), 400

            scrim["team_b"] = team_b
            scrim["opponent"] = team_b
            scrim["status"] = "scheduled"

            return jsonify({
                "success": True,
                "scrim": scrim
            })

    return jsonify({"error": "Scrim not found."}), 404


# ==========================================================
# ANNOUNCEMENTS
# ==========================================================
@app.route("/announce", methods=["POST"])
def announce():
    data = request.json or {}

    message = data.get("message", "").strip()
    channel = data.get("channel")
    user_id = data.get("user_id")

    if not is_owner(user_id):
        return jsonify({"error": "Only the owner can send announcements."}), 403

    if not message:
        return jsonify({"error": "Message cannot be empty."}), 400

    channel_id = 0

    if channel == "transactions":
        channel_id = TRANSACTIONS_CHANNEL_ID
    elif channel == "new-teams":
        channel_id = NEW_TEAMS_CHANNEL_ID

    if channel_id:
        run_bot_task(send_channel_message(channel_id, message))
    else:
        print(f"Announcement would send to {channel}: {message}")

    return jsonify({
        "success": True,
        "message": "Announcement sent."
    })


# ==========================================================
# SUSPENSIONS
# ==========================================================
@app.route("/suspensions/unsuspend-player", methods=["POST"])
def unsuspend_player():
    data = request.json or {}

    user_id = str(data.get("user_id"))
    moderator_id = data.get("moderator_id")

    if not is_owner(moderator_id):
        return jsonify({"error": "Only the owner can lift suspensions."}), 403

    if database_enabled():
        return jsonify(db_unsuspend("player", user_id))

    memory_suspensions["players"].pop(user_id, None)

    return jsonify({
        "success": True,
        "message": "Player suspension lifted."
    })


@app.route("/suspensions/unsuspend-team", methods=["POST"])
def unsuspend_team():
    data = request.json or {}

    team_name = data.get("team_name")
    moderator_id = data.get("moderator_id")

    if not is_owner(moderator_id):
        return jsonify({"error": "Only the owner can lift suspensions."}), 403

    if database_enabled():
        return jsonify(db_unsuspend("team", team_name))

    memory_suspensions["teams"].pop(team_name, None)

    return jsonify({
        "success": True,
        "message": "Team suspension lifted."
    })


# ==========================================================
# RUN FLASK + BOT
# ==========================================================
def run_flask():
    port = int(os.getenv("PORT", 5000))
    print(f"Starting Flask on port {port}")
    app.run(host="0.0.0.0", port=port)


def main():
    init_db()

    if DISCORD_BOT_TOKEN:
        flask_thread = threading.Thread(target=run_flask)
        flask_thread.daemon = True
        flask_thread.start()

        bot.run(DISCORD_BOT_TOKEN)
    else:
        print("DISCORD_BOT_TOKEN is missing. Running website/API only.")
        run_flask()


if __name__ == "__main__":
    main()
