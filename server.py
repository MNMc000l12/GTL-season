import os
import threading
import asyncio
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import discord
import psycopg2
import psycopg2.extras
from discord.ext import commands

app = Flask(__name__)
CORS(app)

# ==========================================================
# ENVIRONMENT VARIABLES
# These come from Railway Variables, not GitHub.
# ==========================================================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "1102370626796261498"))
OWNER_ID = int(os.getenv("OWNER_ID", "779805628804890644"))

TEAM_MEMBER_ROLE_ID = int(os.getenv("TEAM_MEMBER_ROLE_ID", "1218334966341959750"))
CAPTAIN_ROLE_ID = int(os.getenv("CAPTAIN_ROLE_ID", "0"))
SEASON_MANAGER_ROLE_ID = int(os.getenv("SEASON_MANAGER_ROLE_ID", "1102378245216813127"))

# Optional channel IDs. Set these in Railway later if you want announcements.
TRANSACTIONS_CHANNEL_ID = int(os.getenv("TRANSACTIONS_CHANNEL_ID", "0"))
NEW_TEAMS_CHANNEL_ID = int(os.getenv("NEW_TEAMS_CHANNEL_ID", "0"))

# ==========================================================
# TEMPORARY DATA STORAGE
# Warning: This resets if Railway restarts.
# Later upgrade this to Railway PostgreSQL or Supabase.
# ==========================================================
teams = {}
scrims = []
season = None
suspensions = {
    "players": {},
    "teams": {}
}

# ==========================================================
# DISCORD BOT SETUP
# ==========================================================
intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Discord bot logged in as {bot.user}")


def run_bot_task(coro):
    """
    Safely sends an async Discord task to the bot event loop.
    This avoids the classic 'there is no current event loop' nonsense.
    """
    if bot.loop and bot.loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, bot.loop)
    else:
        print("Bot loop is not running yet.")


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


def find_user_team(user_id):
    user_id = int(user_id)

    for team_name, team in teams.items():
        if user_id in team["members"]:
            return team_name, team

    return None, None


def user_is_captain_or_cocaptain(user_id, team):
    user_id = int(user_id)
    return user_id == team.get("captain") or user_id == team.get("co_captain")
    
    DATABASE_URL = os.getenv("DATABASE_URL")


def get_db_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing. Add PostgreSQL to Railway and connect it to this service.")
    return psycopg2.connect(DATABASE_URL)


def init_db():
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


# ==========================================================
# WEBSITE ROUTE
# This makes Railway show your actual GTL website at /
# ==========================================================
@app.route("/")
def home():
    return send_from_directory(".", "index.html")


# API status test route
@app.route("/api/status")
def api_status():
    return jsonify({
        "status": "GTL API is running",
        "message": "The backend is alive. Somehow."
    })


# ==========================================================
# BASIC API ROUTES
# ==========================================================
@app.route("/teams", methods=["GET"])
def get_teams():
    return jsonify(teams)


@app.route("/scrims", methods=["GET"])
def get_scrims():
    return jsonify(scrims)


@app.route("/suspensions", methods=["GET"])
def get_suspensions():
    return jsonify(suspensions)


@app.route("/season/current", methods=["GET"])
def get_current_season():
    if season is None:
        return jsonify({})
    return jsonify(season)


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

    if name in teams:
        return jsonify({"error": "That team already exists."}), 400

    existing_team_name, _ = find_user_team(captain_id)
    if existing_team_name:
        return jsonify({"error": "You are already on a team."}), 400

    teams[name] = {
        "abbreviation": abbreviation,
        "captain": captain_id,
        "co_captain": None,
        "members": [captain_id],
        "official": False,
        "logo_url": logo_url,
        "team_discord": team_discord,
        "captain_username": captain_username
    }

    run_bot_task(assign_team_roles(captain_id))

    return jsonify({
        "success": True,
        "message": f"Team {name} created."
    })


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


@app.route("/teams/official", methods=["POST"])
def make_team_official():
    data = request.json or {}

    team_name = data.get("team_name")
    user_id = data.get("user_id")

    if not is_owner(user_id):
        return jsonify({"error": "Only the owner can make teams official."}), 403

    if team_name not in teams:
        return jsonify({"error": "Team not found."}), 404

    teams[team_name]["official"] = True

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

    if team_name not in teams:
        return jsonify({"error": "Team not found."}), 404

    team = teams[team_name]

    if user_id != team["captain"] and user_id != OWNER_ID:
        return jsonify({"error": "Only the captain or owner can disband this team."}), 403

    del teams[team_name]

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

    if team_name not in teams:
        return jsonify({"error": "Team not found."}), 404

    team = teams[team_name]

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


async def dm_invite(target_id, team_name):
    try:
        user = await bot.fetch_user(int(target_id))
        await user.send(f"You have been invited to join **{team_name}** in GTL.")
        print(f"Invite sent to {target_id}")
    except Exception as e:
        print(f"Could not DM user: {e}")


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

    if old_name not in teams:
        return jsonify({"error": "Team not found."}), 404

    team = teams[old_name]

    if not user_is_captain_or_cocaptain(user_id, team) and user_id != OWNER_ID:
        return jsonify({"error": "Only captains, co-captains, or the owner can edit this team."}), 403

    final_name = new_name.strip() if new_name else old_name

    if final_name != old_name and final_name in teams:
        return jsonify({"error": "A team with that name already exists."}), 400

    if abbreviation:
        team["abbreviation"] = abbreviation.strip().upper()

    if logo_url is not None:
        team["logo_url"] = logo_url

    if team_discord is not None:
        team["team_discord"] = team_discord

    if final_name != old_name:
        teams[final_name] = team
        del teams[old_name]

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

    new_scrim = {
        "id": str(len(scrims) + 1),
        "team_a": team_a,
        "team_b": opponent if opponent else None,
        "opponent": opponent if opponent else None,
        "date": date,
        "time": time,
        "notes": notes,
        "created_by": created_by,
        "status": "scheduled" if opponent else "open"
    }

    scrims.append(new_scrim)

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

    for scrim in scrims:
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
# SUSPENSIONS
# ==========================================================
@app.route("/suspensions/unsuspend-player", methods=["POST"])
def unsuspend_player():
    data = request.json or {}

    user_id = str(data.get("user_id"))
    moderator_id = data.get("moderator_id")

    if not is_owner(moderator_id):
        return jsonify({"error": "Only the owner can lift suspensions."}), 403

    suspensions["players"].pop(user_id, None)

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

    suspensions["teams"].pop(team_name, None)

    return jsonify({
        "success": True,
        "message": "Team suspension lifted."
    })


# ==========================================================
# RUN FLASK + BOT
# ==========================================================
def run_flask():
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    if not DISCORD_BOT_TOKEN:
        print("Missing DISCORD_BOT_TOKEN. Add it in Railway Variables.")
    else:
        bot.run(DISCORD_BOT_TOKEN)
