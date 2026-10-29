import os
import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import discord
import yt_dlp
from discord.ext import commands
from discord.ui import View
from dotenv import load_dotenv

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
OPUS_BITRATE = 128000

queues = {}
history = {}
voice_clients = {}
loop_modes = {}

# yt-dlp: satu ekstraksi pada satu waktu agar hemat CPU/RAM
_BASE_YDL_OPTS = {
    'quiet': True,
    'no_warnings': True,
    'cachedir': False,
    'socket_timeout': 15,
    'source_address': '0.0.0.0',
}

SINGLE_YDL_OPTS = {
    **_BASE_YDL_OPTS,
    'format': 'bestaudio/best',
    'noplaylist': True,
    'default_search': 'ytsearch1',
    'extract_flat': False,
}

PLAYLIST_YDL_OPTS = {
    **_BASE_YDL_OPTS,
    'format': 'bestaudio/best',
    'noplaylist': False,
    'extract_flat': 'in_playlist',
    'playlistend': MAX_QUEUE,
    'ignoreerrors': True,
}

_YDL_LOCK = threading.Lock()

# FFmpeg optimized for low CPU - no resampling filter
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -probesize 32 -analyzeduration 0 -thread_queue_size 128',
    'options': '-vn -b:a 128k -ar 48000 -ac 2',
}


def _is_youtube_host(host):
    host = (host or '').lower()
    return host == 'youtube.com' or host.endswith('.youtube.com') or host == 'youtu.be'


def _normalize_track_url(url, video_id=None):
    """Ubah link YouTube/YouTube Music menjadi URL video yang bersih."""
    if not url:
        return f"https://www.youtube.com/watch?v={video_id}" if video_id else ""

    if not url.startswith(('http://', 'https://')):
        return f"https://www.youtube.com/watch?v={video_id}" if video_id else url

    parsed = urlparse(url)
    host = parsed.netloc.lower()

    if host == 'youtu.be':
        video_id = parsed.path.strip('/').split('/')[0] or video_id
    elif _is_youtube_host(host):
        video_id = parse_qs(parsed.query).get('v', [None])[0] or video_id

    if video_id and _is_youtube_host(host):
        return f"https://www.youtube.com/watch?v={video_id}"
    return url


def is_playlist_url(query):
    """Deteksi link playlist YouTube / YouTube Music."""
    if not query.startswith(('http://', 'https://')):
        return False

    parsed = urlparse(query)
    host = parsed.netloc.lower()
    if not _is_youtube_host(host) or host == 'youtu.be':
        return False

    query_params = parse_qs(parsed.query)
    is_playlist_path = parsed.path.rstrip('/') == '/playlist'
    is_list_only = 'list' in query_params and 'v' not in query_params
    return is_playlist_path or is_list_only


def _extract_info(query, options):
    with _YDL_LOCK:
        with yt_dlp.YoutubeDL(options) as ydl:
            return ydl.extract_info(query, download=False)


def extract_single(query):
    """Ambil satu lagu (URL video, link YouTube Music, atau teks pencarian)."""
    info = _extract_info(query, SINGLE_YDL_OPTS)
    if not info:
        raise RuntimeError("Sumber tidak ditemukan")

    if info.get('entries') is not None:
        entries = [entry for entry in info['entries'] if entry]
        if not entries:
            raise RuntimeError("Tidak ada hasil yang bisa diputar")
        info = entries[0]

    return info


def extract_playlist(query):
    """Ambil metadata playlist tanpa mengunduh audio."""
    info = _extract_info(query, PLAYLIST_YDL_OPTS)
    if not info:
        return [], None

    title = info.get('title') or 'Playlist'
    entries = info.get('entries')
    if entries is None:
        return [Track.from_info(info, fallback_url=query)], title

    tracks = []
    for entry in entries:
        if not entry or entry.get('_type') in ('playlist', 'multi_video'):
            continue
        try:
            tracks.append(Track.from_info(entry, fallback_url=query))
        except Exception:
            continue
        if len(tracks) >= MAX_QUEUE:
            break

    return tracks, title


@dataclass(slots=True)
class Track:
    title: str
    url: str
    duration: int | None = None
    source: str = 'YouTube'

    @classmethod
    def from_info(cls, info, fallback_url=''):
        raw_urls = ' '.join(
            str(info.get(key) or '')
            for key in ('webpage_url', 'original_url', 'url', 'webpage_url_domain')
        )
        raw_url = (
            info.get('webpage_url')
            or info.get('original_url')
            or info.get('url')
            or fallback_url
        )
        url = _normalize_track_url(raw_url, info.get('id'))
        if not url:
            raise ValueError("URL lagu tidak ditemukan")

        duration = info.get('duration')
        try:
            duration = int(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration = None

        source = 'YouTube Music' if 'music.youtube.com' in raw_urls else 'YouTube'

        return cls(
            title=str(info.get('title') or info.get('fulltitle') or 'Unknown'),
            url=url,
            duration=duration,
            source=source,
        )


class YTDLSource(discord.PCMVolumeTransformer):
    __slots__ = ('data', 'title', 'url', 'duration')

    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title', 'Unknown')
        self.url = data.get('webpage_url', '')
        self.duration = data.get('duration')

    @classmethod
    async def from_track(cls, track, *, loop=None):
        loop = loop or asyncio.get_running_loop()
        info = await loop.run_in_executor(None, extract_single, track.url)
        stream_url = info.get('url')
        if not stream_url:
            raise RuntimeError("Stream audio tidak ditemukan")
        return cls(discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS), data=info)

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

    current = getattr(vc, 'current_track', None)
    is_loop = get_loop_mode(guild_id) == 'song' and current is not None
    track = current if is_loop else (queue.popleft() if queue else None)

    if track is None:
        await vc.disconnect()
        cleanup_guild(guild_id)
        return

    try:
        source = await YTDLSource.from_track(track)
    except Exception as exc:
        await ctx.send(f"⚠️ Gagal memutar **{track.title}**: `{exc}`")
        if is_loop:
            loop_modes[guild_id] = 'off'
        await play_next(ctx)
        return

    if not is_loop and current is not None:
        hist.append(current)

    vc.current_track = track
    vc.current_source = source
    vc.play(source, after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop))

    embed = discord.Embed(
        title="🎵 Sekarang Diputar",
        description=f"**[{track.title}]({track.url})**",
        color=0x00ff00
    )
    embed.set_footer(text=f"Sumber: {track.source}")
    if track.duration:
        m, s = divmod(track.duration, 60)
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
    if not vc:
        return

    queue = get_queue(ctx.guild.id)
    if len(queue) >= MAX_QUEUE:
        return await ctx.send(f"❌ Antrean penuh ({MAX_QUEUE} lagu).")

    loop = asyncio.get_running_loop()
    async with ctx.typing():
        try:
            if is_playlist_url(query):
                tracks, playlist_title = await loop.run_in_executor(None, extract_playlist, query)
            else:
                info = await loop.run_in_executor(None, extract_single, query)
                tracks = [Track.from_info(info, fallback_url=query)]
                playlist_title = None
        except Exception as e:
            return await ctx.send(f"❌ Gagal membaca sumber: `{e}`")

    if not tracks:
        return await ctx.send("❌ Playlist kosong atau tidak bisa dibaca.")

    space = MAX_QUEUE - len(queue)
    added = tracks[:space]
    for track in added:
        queue.append(track)

    if not vc.is_playing() and not vc.is_paused():
        await play_next(ctx)
        if len(added) > 1:
            await ctx.send(f"📋 **{playlist_title}** — {len(added)} lagu masuk antrean.")
    elif len(added) == 1:
        track = added[0]
        embed = discord.Embed(
            title="✅ Ditambahkan",
            description=f"**[{track.title}]({track.url})**",
            color=0x00ffff
        )
        embed.set_footer(text=f"Sumber: {track.source}")
        await ctx.send(embed=embed)
    else:
        embed = discord.Embed(
            title="📋 Playlist Ditambahkan",
            description=f"**{playlist_title}**\n{len(added)} lagu masuk antrean.",
            color=0x00ffff
        )
        await ctx.send(embed=embed)

    if len(tracks) > len(added):
        await ctx.send(f"⚠️ Antrean penuh. Hanya {len(added)} dari {len(tracks)} lagu ditambahkan.")

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

    current = getattr(vc, 'current_track', None) if vc else None
    if current:
        queue.appendleft(current)
    queue.appendleft(hist.pop())

    loop_modes[ctx.guild.id] = 'off'
    if vc:
        vc.stop()
    else:
        await play_next(ctx)
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
    e.add_field(name="🎵 Music (All Members)", value="`!bj <judul/URL>` - Play/Queue (YouTube & YouTube Music)\n`!bj <link playlist>` - Tambah semua lagu playlist\n`!bjx`/`!bjs` - Skip\n`!bjq`/`!queue` - Queue + buttons\n`!bjpause`/`!bjz` - Pause\n`!bjr` - Resume/Repeat\n`!bjls` - Loop toggle\n`!bjprev` - Previous\n`!bjstop` - Stop & clear\n`!bjdc`/`!bjoff` - Disconnect", inline=False)
    e.add_field(name="⚙️ Audio", value="`!bjbitrate [64-384]` - Set bitrate\n`!bjregion [region]` - Set voice region", inline=False)
    return e

@bot.tree.command(name="helpbj", description="Panduan lengkap command")
async def slash_helpbj(interaction):
    await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)

@bot.command(name="helpbj")
async def prefix_helpbj(ctx):
    await ctx.send(embed=build_help_embed())

bot.run(TOKEN)