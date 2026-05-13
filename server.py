import os
@app.route("/")
def home():
    return jsonify({
        "status": "GTL API is running",
        "message": "The backend is alive. Somehow."
    })
from flask_cors import CORS
import discord
from discord.ext import commands
import threading

app = Flask(__name__)
CORS(app)

# Secret values come from Railway environment variables
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "1102370626796261498"))
OWNER_ID = int(os.getenv("OWNER_ID", "779805628804890644"))

TEAM_MEMBER_ROLE_ID = int(os.getenv("TEAM_MEMBER_ROLE_ID", "1218334966341959750"))
CAPTAIN_ROLE_ID = int(os.getenv("CAPTAIN_ROLE_ID", "0"))

# Temporary in-memory data.
# Later, you should use a real database.
teams = {}
scrims = []
season = None
suspensions = {
    "players": {},
    "teams": {}
}

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"Discord bot logged in as {bot.user}")


def is_owner(user_id):
    return int(user_id) == OWNER_ID


@app.route("/")
def home():
    return jsonify({
        "status": "GTL API is running",
        "message": "The backend is alive. Somehow."
    })


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


@app.route("/teams/create", methods=["POST"])
def create_team():
    data = request.json

    name = data.get("name")
    abbreviation = data.get("abbreviation")
    captain_id = int(data.get("captain_id"))
    captain_username = data.get("captain_username")
    logo_url = data.get("logo_url")
    team_discord = data.get("team_discord")

    if not name or not abbreviation:
        return jsonify({"error": "Team name and abbreviation are required."}), 400

    if name in teams:
        return jsonify({"error": "That team already exists."}), 400

    # Check if user is already on a team
    for team in teams.values():
        if captain_id in team["members"]:
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

    # Try to assign Discord role
    bot.loop.create_task(assign_team_roles(captain_id))

    return jsonify({
        "success": True,
        "message": f"Team {name} created."
    })


async def assign_team_roles(user_id):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        print("Guild not found.")
        return

    member = guild.get_member(int(user_id))
    if not member:
        print("Member not found.")
        return

    team_role = guild.get_role(TEAM_MEMBER_ROLE_ID)
    captain_role = guild.get_role(CAPTAIN_ROLE_ID) if CAPTAIN_ROLE_ID != 0 else None

    roles_to_add = []

    if team_role:
        roles_to_add.append(team_role)

    if captain_role:
        roles_to_add.append(captain_role)

    if roles_to_add:
        await member.add_roles(*roles_to_add)
        print(f"Roles added to {member}")


@app.route("/teams/official", methods=["POST"])
def make_team_official():
    data = request.json
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
    data = request.json
    team_name = data.get("team_name")
    user_id = int(data.get("user_id"))

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


@app.route("/teams/invite", methods=["POST"])
def invite_player():
    data = request.json
    team_name = data.get("team_name")
    target_id = int(data.get("target_id"))
    inviter_id = int(data.get("inviter_id"))

    if team_name not in teams:
        return jsonify({"error": "Team not found."}), 404

    team = teams[team_name]

    if inviter_id != team["captain"] and inviter_id != team.get("co_captain"):
        return jsonify({"error": "Only captains or co-captains can invite players."}), 403

    bot.loop.create_task(dm_invite(target_id, team_name))

    return jsonify({
        "success": True,
        "message": "Invite sent."
    })


async def dm_invite(target_id, team_name):
    try:
        user = await bot.fetch_user(int(target_id))
        await user.send(f"You have been invited to join **{team_name}** in GTL.")
    except Exception as e:
        print(f"Could not DM user: {e}")


@app.route("/scrims/create", methods=["POST"])
def create_scrim():
    data = request.json

    new_scrim = {
        "id": str(len(scrims) + 1),
        "team_a": data.get("team_a"),
        "team_b": None,
        "opponent": data.get("opponent"),
        "date": data.get("date"),
        "time": data.get("time"),
        "notes": data.get("notes"),
        "created_by": data.get("created_by"),
        "status": "scheduled" if data.get("opponent") else "open"
    }

    if data.get("opponent"):
        new_scrim["team_b"] = data.get("opponent")

    scrims.append(new_scrim)

    return jsonify({
        "success": True,
        "scrim": new_scrim
    })


@app.route("/scrims/accept", methods=["POST"])
def accept_scrim():
    data = request.json
    scrim_id = data.get("scrim_id")
    team_b = data.get("team_b")

    for scrim in scrims:
        if scrim["id"] == scrim_id:
            if scrim["status"] != "open":
                return jsonify({"error": "This scrim is not open."}), 400

            scrim["team_b"] = team_b
            scrim["opponent"] = team_b
            scrim["status"] = "scheduled"

            return jsonify({
                "success": True,
                "scrim": scrim
            })

    return jsonify({"error": "Scrim not found."}), 404


@app.route("/announce", methods=["POST"])
def announce():
    data = request.json
    message = data.get("message")
    user_id = data.get("user_id")

    if not is_owner(user_id):
        return jsonify({"error": "Only the owner can send announcements."}), 403

    # Later you can connect this to a specific Discord channel.
    print(f"Announcement: {message}")

    return jsonify({
        "success": True,
        "message": "Announcement sent."
    })


def run_flask():
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.start()

    if not DISCORD_BOT_TOKEN:
        print("Missing DISCORD_BOT_TOKEN.")
    else:
        bot.run(DISCORD_BOT_TOKEN)
