import os
import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
import yt_dlp
from collections import deque

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)

queues = {}
voice_clients = {}
loop_modes = {}

YDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
}

FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')
        self.duration = data.get('duration')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=False):
        loop = loop or asyncio.get_event_loop()
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            data = await loop.run_in_executor(None, lambda: ydl.extract_info(url, download=not stream))
        if 'entries' in data:
            data = data['entries'][0]
        filename = data['url'] if stream else ydl.prepare_filename(data)
        return cls(discord.FFmpegPCMAudio(filename, **FFMPEG_OPTIONS), data=data)

def get_queue(guild_id):
    if guild_id not in queues:
        queues[guild_id] = deque()
    return queues[guild_id]

def get_loop_mode(guild_id):
    return loop_modes.get(guild_id, 'off')

async def play_next(ctx):
    guild_id = ctx.guild.id
    queue = get_queue(guild_id)
    vc = voice_clients.get(guild_id)

    if not vc or not vc.is_connected():
        return

    if not queue:
        await vc.disconnect()
        voice_clients.pop(guild_id, None)
        return

    if get_loop_mode(guild_id) == 'song' and hasattr(vc, 'current_source'):
        source = vc.current_source
    else:
        source = queue.popleft()
        if get_loop_mode(guild_id) == 'song':
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
    await ctx.send(embed=embed)

@bot.event
async def on_ready():
    print(f"Bot aktif sebagai {bot.user}")

async def ensure_voice(ctx):
    if ctx.author.voice is None:
        await ctx.send("Kamu harus di voice channel dulu!")
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
    """Play lagu atau masukkan ke antrean"""
    vc = await ensure_voice(ctx)
    if not vc:
        return

    async with ctx.typing():
        try:
            source = await YTDLSource.from_url(query, loop=bot.loop, stream=True)
        except Exception as e:
            await ctx.send(f"Error: {e}")
            return

    queue = get_queue(ctx.guild.id)
    queue.append(source)

    if not vc.is_playing() and not vc.is_paused():
        await play_next(ctx)
    else:
        embed = discord.Embed(
            title="✅ Ditambahkan ke Antrean",
            description=f"**[{source.title}]({source.url})**",
            color=0x00ffff
        )
        await ctx.send(embed=embed)

@bot.command(aliases=['bjs'])
async def bjx(ctx):
    """Skip lagu"""
    vc = ctx.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()
        await ctx.send("⏭️ Lagu diskip!")
    else:
        await ctx.send("Tidak ada lagu yang diputar.")

@bot.command(aliases=['bjq'])
async def bjq(ctx):
    """Lihat antrean"""
    queue = get_queue(ctx.guild.id)
    if not queue:
        await ctx.send("Antrean kosong.")
        return

    embed = discord.Embed(title="🎶 Antrean Lagu", color=0x00ffff)
    for i, source in enumerate(list(queue)[:10], 1):
        embed.add_field(name=f"{i}. {source.title}", value=source.url, inline=False)
    if len(queue) > 10:
        embed.set_footer(text=f"Dan {len(queue) - 10} lagu lagi...")
    await ctx.send(embed=embed)

@bot.command(aliases=['bjz'])
async def bjpause(ctx):
    """Pause lagu"""
    vc = ctx.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.send("⏸️ Lagu dijeda.")
    else:
        await ctx.send("Tidak ada lagu yang diputar.")

@bot.command()
async def bjr(ctx):
    """Resume / Repeat (putar ulang lagu sekarang)"""
    vc = ctx.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.send("▶️ Lagu dilanjutkan.")
    elif vc and vc.is_playing() and hasattr(vc, 'current_source'):
        vc.stop()
        await ctx.send("🔁 Lagu diulang dari awal.")
    else:
        await ctx.send("Tidak ada lagu untuk diulang.")

@bot.command()
async def bjls(ctx):
    """Loop song on/off"""
    guild_id = ctx.guild.id
    current = loop_modes.get(guild_id, 'off')
    loop_modes[guild_id] = 'off' if current == 'song' else 'song'
    status = "nyala" if loop_modes[guild_id] == 'song' else "mati"
    await ctx.send(f"🔁 Loop lagu: **{status}**")

@bot.command(aliases=['bjoff'])
async def bjdc(ctx):
    """Disconnect bot dari voice"""
    vc = ctx.voice_client
    if vc:
        queues.pop(ctx.guild.id, None)
        loop_modes.pop(ctx.guild.id, None)
        await vc.disconnect()
        voice_clients.pop(ctx.guild.id, None)
        await ctx.send("👋 Bot keluar dari voice channel.")
    else:
        await ctx.send("Bot tidak di voice channel.")

bot.run(TOKEN)