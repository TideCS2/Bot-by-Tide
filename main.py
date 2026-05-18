import discord
import os
from dotenv import load_dotenv
from discord import app_commands
from discord.ext import commands
import aiohttp
import aiosqlite
import urllib.parse
import asyncio
from aiohttp import web

load_dotenv()




# =========================
# CONFIG
# =========================

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")

REDIRECT_URI = "https://bot-by-tide-production.up.railway.app/callback"

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

    # ADD THIS (required)
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

    print("Twitch response:", data)

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
# DISCORD COMMAND: VERIFY
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

    url = "https://id.twitch.tv/oauth2/authorize?" + urllib.parse.urlencode(params)

    await interaction.response.send_message(
        f"Verify Twitch here:\n{url}",
        ephemeral=True
    )

# =========================
# MOD: ADD CHANNEL
# =========================

@bot.tree.command(name="add_channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def add_channel(interaction: discord.Interaction, twitch_username: str, role: discord.Role):

    await interaction.response.defer(ephemeral=True)

    twitch_id = await get_channel_id(twitch_username)

    if not twitch_id:
        await interaction.followup.send("❌ Twitch channel not found.")
        return

    await db.execute(
        "INSERT INTO channels VALUES (?, ?, ?, ?)",
        (interaction.guild.id, twitch_username, twitch_id, role.id)
    )
    await db.commit()

    await interaction.followup.send(
        f"✅ Added {twitch_username} → {role.name}"
    )

# =========================
# MOD: REMOVE CHANNEL
# =========================

@bot.tree.command(name="remove_channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def remove_channel(interaction: discord.Interaction, twitch_username: str):

    await db.execute(
        "DELETE FROM channels WHERE guild_id=? AND twitch_channel=?",
        (interaction.guild.id, twitch_username)
    )
    await db.commit()

    await interaction.response.send_message("Removed channel", ephemeral=True)

# =========================
# LIST CHANNELS
# =========================

@bot.tree.command(name="list_channels")
async def list_channels(interaction: discord.Interaction):

    cursor = await db.execute(
        "SELECT twitch_channel, role_id FROM channels WHERE guild_id=?",
        (interaction.guild.id,)
    )

    rows = await cursor.fetchall()

    msg = "\n".join([f"{c} → <@&{r}>" for c, r in rows]) or "No channels set."

    await interaction.response.send_message(msg, ephemeral=True)

# =========================
# ROLE SYNC ENGINE
# =========================

async def assign_roles(discord_id, twitch_access_token):

    print("Running assign_roles for:", discord_id)

    user_data = await get_twitch_user(twitch_access_token)
    twitch_user_id = user_data["id"]

    async with db.execute("SELECT * FROM channels") as cursor:
        channels = await cursor.fetchall()

    for guild_id, channel, broadcaster_id, role_id in channels:

        try:
            print(f"Checking guild {guild_id}")

            is_following = await check_follow(
                twitch_user_id,
                broadcaster_id,
                twitch_access_token
            )

            guild = bot.get_guild(guild_id)

            if not guild:
                print("Guild not found")
                continue

            member = await guild.fetch_member(discord_id)

            role = guild.get_role(role_id)

            if not member:
                print("Member not found")
                continue

            if not role:
                print("Role not found")
                continue

            print("Follow status:", is_following)

            if is_following:

                if role not in member.roles:
                    await member.add_roles(role)
                    print("ROLE ASSIGNED")

            else:
                print("Not following")

        except Exception as e:
            print("assign_roles error:", e)


async def sync_roles():

    await bot.wait_until_ready()

    while not bot.is_closed():

        async with db.execute("SELECT * FROM users") as cursor:
            users = await cursor.fetchall()

        for discord_id, twitch_id, twitch_name, token in users:

            await assign_roles(discord_id, token)

        await asyncio.sleep(300)

# =========================
# OAUTH WEB SERVER (NO FLASK)
# =========================

import os
from aiohttp import web

routes = web.RouteTableDef()


@routes.get("/")
async def home(request):
    return web.Response(text="Bot is running")


@routes.get("/callback")
async def callback(request):

    code = request.query.get("code")
    state = request.query.get("state")

    if not code:
        return web.Response(text="Missing code from Twitch")

    # If state missing (safety check)
    if not state:
        return web.Response(text="Missing state")

    discord_id = int(state)

    # exchange token
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

    access_token = token_data.get("access_token")

    if not access_token:
        return web.Response(text=f"Token error: {token_data}")

    user = await get_twitch_user(access_token)

    await db.execute("""
        INSERT OR REPLACE INTO users VALUES (?, ?, ?, ?)
    """, (discord_id, user["id"], user["login"], access_token))

    await db.commit()

    # ASSIGN ROLES IMMEDIATELY
    await assign_roles(discord_id, access_token)

    return web.Response(
        text="Verified! Your Discord roles have been updated. You can close this tab."
    )

# =========================
# START WEB SERVER
# =========================

import os
from aiohttp import web

routes = web.RouteTableDef()


@routes.get("/")
async def home(request):
    return web.Response(text="Bot is running")


@routes.get("/callback")
async def callback(request):

    code = request.query.get("code")
    state = request.query.get("state")

    if not code:
        return web.Response(text="Missing code from Twitch")

    return web.Response(text="SUCCESS: Twitch callback reached")


async def start_web():
    app = web.Application()
    app.add_routes(routes)

    runner = web.AppRunner(app)
    await runner.setup()

    port = int(os.getenv("PORT", 8080))  # safer default for Railway

    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

    print(f"Web server running on port {port}")

# =========================
# READY EVENT
# =========================

@bot.event
async def on_ready():
    await init_db()
    await bot.tree.sync()

    print(f"Logged in as {bot.user}")

    bot.loop.create_task(sync_roles())
    bot.loop.create_task(start_web())

# =========================
# RUN BOT
# =========================

bot.run(DISCORD_TOKEN)