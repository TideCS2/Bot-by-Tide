import os
import discord
from discord import app_commands
from discord.ext import commands
import aiohttp
import aiosqlite
import asyncio
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# =========================
# CONFIG
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

# IMPORTANT: CHANGE THIS AFTER DEPLOY
REDIRECT_URI = os.getenv("REDIRECT_URI")

# =========================
# DISCORD SETUP
# =========================

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

db = None

# =========================
# DATABASE
# =========================

async def init_db():
    global db
    db = await aiosqlite.connect("data.db")

    await db.executescript("""
    CREATE TABLE IF NOT EXISTS channels (
        guild_id INTEGER,
        twitch_channel TEXT,
        twitch_id TEXT,
        role_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS users (
        discord_id INTEGER PRIMARY KEY,
        twitch_id TEXT,
        twitch_name TEXT,
        access_token TEXT
    );
    """)

    await db.commit()

# =========================
# TWITCH HELPERS
# =========================

async def twitch_request(url, token=None, params=None):
    headers = {
        "Client-ID": TWITCH_CLIENT_ID
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, params=params) as r:
            return await r.json()


async def get_twitch_user(token):
    data = await twitch_request("https://api.twitch.tv/helix/users", token)
    return data["data"][0]


async def get_channel_id(username):
    token = await get_app_token()

    data = await twitch_request(
        "https://api.twitch.tv/helix/users",
        token=token,
        params={"login": username}
    )

    if not data.get("data"):
        return None

    return data["data"][0]["id"]


async def get_app_token():
    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "grant_type": "client_credentials"
            }
        ) as r:
            data = await r.json()
            return data["access_token"]


async def check_follow(user_id, broadcaster_id, token):
    data = await twitch_request(
        "https://api.twitch.tv/helix/channels/followed",
        token,
        params={"user_id": user_id, "broadcaster_id": broadcaster_id}
    )
    return data.get("total", 0) > 0

# =========================
# ROLE ASSIGNMENT
# =========================

async def assign_roles(discord_id, twitch_token):

    try:
        user = await get_twitch_user(twitch_token)
        twitch_user_id = user["id"]
    except Exception as e:
        print("Twitch user fetch failed:", e)
        return

    async with db.execute("SELECT * FROM channels") as cursor:
        channels = await cursor.fetchall()

    for guild_id, channel, broadcaster_id, role_id in channels:

        try:
            guild = bot.get_guild(guild_id)
            if not guild:
                continue

            member = await guild.fetch_member(discord_id)
            role = guild.get_role(role_id)

            if not member or not role:
                continue

            is_following = await check_follow(
                twitch_user_id,
                broadcaster_id,
                twitch_token
            )

            if is_following and role not in member.roles:
                await member.add_roles(role)
                print(f"Assigned role {role.name} to {member}")

        except Exception as e:
            print("assign_roles error:", e)

# =========================
# DISCORD COMMANDS
# =========================

@bot.tree.command(name="verify")
async def verify(interaction: discord.Interaction):

    state = str(interaction.user.id)

    params = {
        "client_id": TWITCH_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "user:read:follows",
        "state": state
    }

    url = "https://id.twitch.tv/oauth2/authorize?" + aiohttp.helpers.urlencode(params)

    await interaction.response.send_message(
        f"Verify here:\n{url}",
        ephemeral=True
    )

@bot.tree.command(name="add_channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def add_channel(interaction: discord.Interaction, twitch_username: str, role: discord.Role):

    await interaction.response.defer(ephemeral=True)

    twitch_id = await get_channel_id(twitch_username)

    if not twitch_id:
        await interaction.followup.send("Channel not found")
        return

    await db.execute(
        "INSERT INTO channels VALUES (?, ?, ?, ?)",
        (interaction.guild.id, twitch_username, twitch_id, role.id)
    )
    await db.commit()

    await interaction.followup.send("Channel added")

# =========================
# WEB SERVER
# =========================

routes = web.RouteTableDef()

@routes.get("/")
async def home(request):
    return web.Response(text="Bot is running")


@routes.get("/callback")
async def callback(request):

    code = request.query.get("code")
    state = request.query.get("state")

    if not code:
        return web.Response(text="Missing code")

    discord_id = int(state)

    async with aiohttp.ClientSession() as session:
        async with session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "client_id": TWITCH_CLIENT_ID,
                "client_secret": TWITCH_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": REDIRECT_URI
            }
        ) as r:
            token_data = await r.json()

    access_token = token_data["access_token"]

    user = await get_twitch_user(access_token)

    await db.execute("""
        INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?)
    """, (discord_id, user["id"], user["login"], access_token))

    await db.commit()

    await assign_roles(discord_id, access_token)

    return web.Response(text="Verified! You can close this tab.")


async def start_web():
    app = web.Application()
    app.add_routes(routes)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", 8080))

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print("Web server running")

# =========================
# READY
# =========================

@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()

    print(f"Logged in as {bot.user}")

    bot.loop.create_task(start_web())

# =========================
# RUN
# =========================

bot.run(DISCORD_TOKEN)