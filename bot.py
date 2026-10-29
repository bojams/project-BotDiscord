import os
import asyncio
import discord
from discord.ext import commands
from discord.ui import View, Button
from dotenv import load_dotenv
import yt_dlp
from collections import deque
from functools import lru_cache

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Minimal intents = less memory/CPU
intents = discord.Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True
intents.voice_states = True

# Disable default help, case-insensitive commands
bot = commands.Bot(
    command_prefix="!", 
    intents=intents, 
    help_command=None,
    case_insensitive=True,
    max_messages=100  # Limit internal message cache
)

# Bounded queues = prevent memory leaks
MAX_QUEUE = 50
MAX_HISTORY = 20

queues = {}
history = {}
voice_clients = {}
loop_modes = {}

# Single shared ytdl instance (thread-safe via executor)
_ytdl = yt_dlp.YoutubeDL({
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'cachedir': False,
    'no_warnings': True,
    'extract_flat': False,
    'socket_timeout': 10,
})

# FFmpeg optimized for low CPU - no resampling filter
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32 -analyzeduration 0 -thread_queue_size 128',
    'options': '-vn -b:a 128k -ar 48000 -ac 2',
}

class YTDLSource(discord.PCMVolumeTransformer):
    __slots__ = ('data', 'title', 'url', 'duration')
    
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title', 'Unknown')
        self.url = data.get('webpage_url', '')
        self.duration = data.get('duration')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()
        # Use shared ytdl instance
        def extract():
            return _ytdl.extract_info(url, download=not stream)
        data = await loop.run_in_executor(None, extract)
        if 'entries' in data:
            data = data['entries'][0]
        data['webpage_url'] = data.get('webpage_url', url)
        filename = data['url'] if stream else _ytdl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS), data=data)

def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = deque(maxlen=MAX_QUEUE)
    return queues[guild_id]

def get_history(guild_id):
    if guild_id not in history:
        history[guild_id] = deque(maxlen=MAX_HISTORY)
    return history[guild_id]

def get_loop_mode(guild_id):
    return loop_modes.get(guild_id, 'off')

def cleanup_guild(guild_id):
    """Cleanup all guild data"""
    queues.pop(guild_id, None)
    history.pop(guild_id, None)
    loop_modes.pop(guild_id, None)
    voice_clients.pop(guild_id, None)

# Periodic cleanup for idle guilds (runs every 10 min)
async def periodic_cleanup():
    await bot.wait_until_ready()
    while not bot.is_closed():
        await asyncio.sleep(600)
        # Clean guilds where bot is not in voice
        for gid in list(queues.keys()):
            vc = voice_clients.get(gid)
            if not vc or not vc.is_connected():
                cleanup_guild(gid)

async def play_next(ctx):
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    hist = get_history(guild_id)
    vc = voice_clients.get(guild_id)

    if not vc or not vc.is_connected():
        cleanup_guild(guild_id)
        return

    if not queue:
        await vc.disconnect()
        cleanup_guild(guild_id)
        return

    if get_loop_mode(guild_id) == 'song' and hasattr(vc, 'current_source'):
        source = vc.current_source
    else:
        if hasattr(vc, 'current_source') and vc.current_source:
            hist.append(vc.current_source)
        source = queue.popleft()
        vc.current_source = source

    vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))

    embed = discord.Embed(
        title="🎵 Sekarang Diputar",
        description=f"**[{source.title}]({source.url})**",
        color=0x00ff00
    )
    if source.duration:
        m, s = divmod(source.duration, 60)
        embed.add_field(name="Durasi", value=f"{m}:{s:02d}")

    view = MusicPlayerView(ctx)
    await ctx.send(embed=embed, view=view)

# ===== UI VIEWS =====
class MusicPlayerView(View):
    __slots__ = ('ctx',)
    
    def __init__(self, ctx):
        super().__init__(timeout=300)
        self.ctx = ctx

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.ctx.author.id

    @discord.ui.button(label="⏮️", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def previous(self, interaction, button):
        await self.ctx.invoke(self.ctx.bot.get_command('bjprev'))
        await interaction.response.defer()

    @discord.ui.button(label="⏹️", style=discord.ButtonStyle.danger, custom_id="stop")
    async def stop(self, interaction, button):
        await self.ctx.invoke(self.ctx.bot.get_command('bjstop'))
        await interaction.response.defer()

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.secondary, custom_id="next")
    async def next(self, interaction, button):
        await self.ctx.invoke(self.ctx.bot.get_command('bjx'))
        await interaction.response.defer()

    @discord.ui.button(label="⏸️", style=discord.ButtonStyle.primary, custom_id="pause")
    async def pause_resume(self, interaction, button):
        vc = self.ctx.voice_client
        if vc and vc.is_playing():
            vc.pause()
            button.label = "▶️"
        elif vc and vc.is_paused():
            vc.resume()
            button.label = "⏸️"
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="🔁", style=discord.ButtonStyle.secondary, custom_id="loop")
    async def toggle_loop(self, interaction, button):
        await self.ctx.invoke(self.ctx.bot.get_command('bjls'))
        await interaction.response.defer()

class QueueView(View):
    __slots__ = ('ctx',)
    
    def __init__(self, ctx):
        super().__init__(timeout=60)
        self.ctx = ctx

    async def interaction_check(self, interaction):
        return interaction.user.id == self.ctx.author.id

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.primary)
    async def skip(self, interaction, button):
        await self.ctx.invoke(self.ctx.bot.get_command('bjx'))
        await interaction.response.defer()

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger)
    async def stop(self, interaction, button):
        await self.ctx.invoke(self.ctx.bot.get_command('bjstop'))
        await interaction.response.defer()

    @discord.ui.button(label="⏮️ Prev", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction, button):
        await self.ctx.invoke(self.ctx.bot.get_command('bjprev'))
        await interaction.response.defer()

# ===== EVENTS =====
@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Sync error: {e}")
    print(f"Bot aktif sebagai {bot.user}")

@bot.tree.error
async def on_app_command_error(interaction, error):
    if isinstance(error, discord.app_commands.CheckFailure):
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ Hanya Owner Server!", ephemeral=True)
    else:
        print(f"Slash Error: {error}")

async def is_guild_owner(interaction):
    return interaction.guild and interaction.user.id == interaction.guild.owner_id

# ===== SLASH: CLEAR =====
@bot.tree.command(name="clear", description="Hapus pesan (Owner only)")
async def slash_clear(interaction: discord.Interaction, jumlah: int = 100):
    if not await is_guild_owner(interaction):
        return await interaction.response.send_message("❌ Owner only!", ephemeral=True)
    if not 1 <= jumlah <= 1000:
        return await interaction.response.send_message("1-1000", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=jumlah + 1)
    await interaction.followup.send(f"✅ Hapus {len(deleted)-1} pesan", ephemeral=True)

@bot.tree.command(name="clearuser", description="Hapus pesan user (Owner only)")
async def slash_clearuser(interaction: discord.Interaction, user: discord.Member, jumlah: int = 100):
    if not await is_guild_owner(interaction):
        return await interaction.response.send_message("❌ Owner only!", ephemeral=True)
    if not 1 <= jumlah <= 1000:
        return await interaction.response.send_message("1-1000", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    deleted = await interaction.channel.purge(limit=jumlah + 1, check=lambda m: m.author == user)
    await interaction.followup.send(f"✅ Hapus {len(deleted)} pesan dari {user.mention}", ephemeral=True)

# ===== PREFIX: CLEAR (OWNER ONLY) =====
def owner_only():
    return commands.check(lambda ctx: ctx.guild and ctx.author.id == ctx.guild.owner_id)

@bot.command()
@owner_only()
async def clear(ctx, jumlah: int = 100):
    if not 1 <= jumlah <= 1000:
        return await ctx.send("1-1000", delete_after=3)
    deleted = await ctx.channel.purge(limit=jumlah + 1)
    await ctx.send(f"✅ Hapus {len(deleted)-1} pesan", delete_after=3)

@bot.command()
@owner_only()
async def clearuser(ctx, user: discord.Member, jumlah: int = 100):
    if not 1 <= jumlah <= 1000:
        return await ctx.send("1-1000", delete_after=3)
    deleted = await ctx.channel.purge(limit=jumlah + 1, check=lambda m: m.author == user or m.id == ctx.message.id)
    await ctx.send(f"✅ Hapus {len(deleted)} pesan dari {user.name}", delete_after=3)

# ===== MUSIC CORE =====
async def ensure_voice(ctx):
    if not ctx.author.voice:
        await ctx.send("❌ Join voice channel dulu!")
        return None
    vc = ctx.voice_client
    if vc is None:
        vc = await ctx.author.voice.channel.connect()
        voice_clients[ctx.guild.id] = vc
    elif vc.channel != ctx.author.voice.channel:
        await vc.move_to(ctx.author.voice.channel)
    return vc

@bot.command(aliases=['bjp'])
async def bj(ctx, *, query: str):
    vc = await ensure_voice(ctx)
    if not vc: return
    
    async with ctx.typing():
        try:
            source = await YTDLSource.from_url(query, loop=bot.loop, stream=True)
        except Exception as e:
            return await ctx.send(f"Error: {e}")
    
    queue = get_queue(ctx.guild.id)
    queue.append(source)

    if not vc.is_playing() and not vc.is_paused():
        await play_next(ctx)
    else:
        embed = discord.Embed(title="✅ Ditambahkan", description=f"**[{source.title}]({source.url})**", color=0x00ffff)
        await ctx.send(embed=embed)

@bot.command(aliases=['bjs'])
async def bjx(ctx):
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await ctx.send("⏭️ Skip!")
    else:
        await ctx.send("Tidak ada lagu.")

@bot.command(name='bjq', aliases=['queue'])
async def bj_queue(ctx):
    queue = get_queue(ctx.guild.id)
    if not queue:
        return await ctx.send("Antrean kosong.")
    embed = discord.Embed(title="🎶 Antrean", color=0x00ffff)
    for i, s in enumerate(list(queue)[:10], 1):
        embed.add_field(name=f"{i}. {s.title}", value=s.url, inline=False)
    if len(queue) > 10:
        embed.set_footer(text=f"+{len(queue)-10} lagi")
    await ctx.send(embed=embed, view=QueueView(ctx))

@bot.command(aliases=['bjz'])
async def bjpause(ctx):
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸️ Pause")
    else:
        await ctx.send("Tidak ada lagu.")

@bot.command()
async def bjr(ctx):
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ Resume")
    elif vc and vc.is_playing() and hasattr(vc, 'current_source'):
        vc.stop()
        await ctx.send("🔁 Repeat")
    else:
        await ctx.send("Tidak ada lagu.")

@bot.command()
async def bjls(ctx):
    gid = ctx.guild.id
    loop_modes[gid] = 'off' if loop_modes.get(gid) == 'song' else 'song'
    await ctx.send(f"🔁 Loop: **{'ON' if loop_modes[gid] == 'song' else 'OFF'}**")

@bot.command(aliases=['bjoff'])
async def bjdc(ctx):
    vc = ctx.voice_client
    if vc:
        cleanup_guild(ctx.guild.id)
        await vc.disconnect()
        await ctx.send("👋 Disconnected")
    else:
        await ctx.send("Bot tidak di VC.")

@bot.command()
async def bjstop(ctx):
    vc = ctx.voice_client
    if vc:
        cleanup_guild(ctx.guild.id)
        vc.stop()
        await vc.disconnect()
        await ctx.send("⏹️ Stopped & cleared")
    else:
        await ctx.send("Bot tidak di VC.")

@bot.command()
async def bjprev(ctx):
    vc = ctx.voice_client
    hist = get_history(ctx.guild.id)
    queue = get_queue(ctx.guild.id)
    if not hist:
        return await ctx.send("History kosong.")
    if vc and (vc.is_playing() or vc.is_paused()) and hasattr(vc, 'current_source') and vc.current_source:
        queue.appendleft(vc.current_source)
    queue.appendleft(hist.pop())
    if vc: vc.stop()
    else: await play_next(ctx)
    await ctx.send("⏮️ Previous")

@bot.command()
async def bjbitrate(ctx, bitrate: int = None):
    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        return await ctx.send("Bot tidak di VC.")
    global OPUS_BITRATE
    if bitrate is None:
        return await ctx.send(f"Bitrate: **{OPUS_BITRATE//1000}kbps**")
    OPUS_BITRATE = max(64, min(384, bitrate)) * 1000
    await ctx.send(f"✅ Bitrate: **{OPUS_BITRATE//1000}kbps**")

@bot.command()
async def bjregion(ctx, region: str = None):
    vc = ctx.voice_client
    if not vc or not vc.is_connected():
        return await ctx.send("Bot tidak di VC.")
    if region is None:
        return await ctx.send(f"Region: **{vc.channel.rtc_region or 'Auto'}**")
    valid = ['singapore','hongkong','japan','sydney','us-west','us-east','us-central','us-south','brazil','europe','russia','india','southafrica','auto']
    if region.lower() not in valid:
        return await ctx.send(f"Invalid. Pilih: {', '.join(valid)}")
    try:
        await vc.channel.edit(rtc_region=None if region.lower() == 'auto' else region.lower())
        await ctx.send(f"✅ Region: **{region}** (perlu rejoin)")
    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

# ===== HELP =====
def build_help_embed():
    e = discord.Embed(title="🤖 Panduan Bot", description="Command, usage, akses:", color=0x5865F2)
    e.add_field(name="🧹 Clear (Owner Only)", value="`/clear [n]` `!clear [n]` - Hapus pesan\n`/clearuser @u [n]` `!clearuser @u [n]` - Hapus pesan user", inline=False)
    e.add_field(name="🎵 Music (All Members)", value="`!bj <judul/URL>` - Play/Queue\n`!bjx`/`!bjs` - Skip\n`!bjq`/`!queue` - Queue + buttons\n`!bjpause`/`!bjz` - Pause\n`!bjr` - Resume/Repeat\n`!bjls` - Loop toggle\n`!bjprev` - Previous\n`!bjstop` - Stop & clear\n`!bjdc`/`!bjoff` - Disconnect", inline=False)
    e.add_field(name="⚙️ Audio", value="`!bjbitrate [64-384]` - Set bitrate\n`!bjregion [region]` - Set voice region", inline=False)
    return e

@bot.tree.command(name="helpbj", description="Panduan lengkap command")
async def slash_helpbj(interaction):
    await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)

@bot.command(name="helpbj")
async def prefix_helpbj(ctx):
    await ctx.send(embed=build_help_embed())

bot.run(TOKEN)