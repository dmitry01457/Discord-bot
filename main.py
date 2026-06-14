import discord
from discord.ext import commands
from discord import app_commands
import os
import asyncio
import io
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime

CONFIG_FILE = "discord-bot/config.json"
LOGO_PATH = "discord-bot/logo.png"

# Попытка загрузить логотип (опционально)
LOGO_BYTES = None
if os.path.exists(LOGO_PATH):
    try:
        with open(LOGO_PATH, "rb") as _f:
            LOGO_BYTES = _f.read()
    except Exception as e:
        print(f"[LOGO] Не удалось загрузить: {e}")

intents = discord.Intents.default()
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ─── Config ───────────────────────────────────────────────────────────────────

_cfg: dict = {}

# channel_id -> {opened_at, user, category, emoji}
ticket_data: dict = {}


def load_cfg() -> dict:
    global _cfg
    if not _cfg and os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            _cfg = json.load(f)
    return _cfg


def save_cfg():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(_cfg, f, ensure_ascii=False, indent=2)


def log_channel(guild_id: int):
    return _cfg.get(str(guild_id), {}).get("log_channel_id")


# ─── Keep-alive ───────────────────────────────────────────────────────────────

class _Ping(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def start_keepalive():
    for port in [9000, 8099, 6800, 6000, 5000]:
        try:
            srv = HTTPServer(("0.0.0.0", port), _Ping)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            print(f"Keep-alive: порт {port}")
            return
        except OSError:
            continue


# ─── Helpers ──────────────────────────────────────────────────────────────────

def staff_role(guild: discord.Guild):
    for name in ("Staff", "Модератор", "Admin", "Администратор"):
        r = discord.utils.get(guild.roles, name=name)
        if r:
            return r
    return None


def get_ticket_roles(guild: discord.Guild, ticket_key: str) -> list[discord.Role]:
    role_ids = _cfg.get(str(guild.id), {}).get("ticket_roles", {}).get(ticket_key, [])
    return [r for rid in role_ids if (r := guild.get_role(rid))]


async def send_log(guild: discord.Guild, embed: discord.Embed):
    cid = log_channel(guild.id)
    if not cid:
        return
    try:
        ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
        await ch.send(embed=embed)
    except Exception as e:
        print(f"[LOG] Ошибка отправки лога (channel={cid}): {e}")


async def create_ticket(
    interaction: discord.Interaction,
    category: str,
    emoji: str,
    fields: list[tuple[str, str]],
    prefix: str = "ticket",
    ticket_key: str = "",
):
    guild = interaction.guild
    user = interaction.user
    safe = user.name.lower().replace(" ", "-").replace("#", "")
    ch_name = f"{prefix}-{safe}"

    if discord.utils.get(guild.text_channels, name=ch_name):
        await interaction.followup.send("У тебя уже открыт тикет!", ephemeral=True)
        return

    ticket_roles = get_ticket_roles(guild, ticket_key) if ticket_key else []
    fallback = staff_role(guild)
    if not ticket_roles and fallback:
        ticket_roles = [fallback]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        user: discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, embed_links=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True),
    }
    for r in ticket_roles:
        overwrites[r] = discord.PermissionOverwrite(read_messages=True, send_messages=True, attach_files=True, embed_links=True)

    if prefix == "набор":
        cat = (discord.utils.get(guild.categories, name="Набор")
               or discord.utils.get(guild.categories, name="Набор на персонал")
               or discord.utils.get(guild.categories, name="Staff"))
    else:
        cat = (discord.utils.get(guild.categories, name="Тикеты")
               or discord.utils.get(guild.categories, name="Tickets")
               or discord.utils.get(guild.categories, name="Support"))
    channel = await guild.create_text_channel(name=ch_name, overwrites=overwrites, category=cat)

    embed = discord.Embed(title=f"{emoji} {category}", color=discord.Color.from_rgb(30, 100, 220), timestamp=datetime.now())
    embed.set_author(name=str(user), icon_url=user.display_avatar.url)
    embed.add_field(name="Пользователь", value=user.mention, inline=True)
    embed.add_field(name="Категория", value=f"{emoji} {category}", inline=True)
    if fields:
        embed.add_field(name="\u200b", value="\u200b", inline=False)
        for label, value in fields:
            if value:
                embed.add_field(name=label, value=value, inline=False)
    embed.set_footer(text="Нажми кнопку чтобы закрыть тикет")

    mention = user.mention
    if ticket_roles:
        mention += " " + " ".join(r.mention for r in ticket_roles)
    
    # Отправляем с логотипом если он есть, иначе без
    if LOGO_BYTES:
        file = discord.File(io.BytesIO(LOGO_BYTES), filename="logo.png")
        embed.set_thumbnail(url="attachment://logo.png")
        await channel.send(content=mention, file=file, embed=embed, view=CloseView())
    else:
        await channel.send(content=mention, embed=embed, view=CloseView())

    opened_at = datetime.now()
    ticket_data[channel.id] = {
        "opened_at": opened_at,
        "user": user,
        "category": f"{emoji} {category}",
    }

    log = discord.Embed(title="📂 Тикет открыт", color=discord.Color.green(), timestamp=opened_at)
    log.set_author(name=str(user), icon_url=user.display_avatar.url)
    log.set_thumbnail(url=user.display_avatar.url)
    log.add_field(name="👤 Пользователь", value=f"{user.mention}\n`{user}` (ID: `{user.id}`)", inline=True)
    log.add_field(name="📁 Категория", value=f"{emoji} {category}", inline=True)
    log.add_field(name="📌 Канал", value=channel.mention, inline=True)
    created_ts = int(user.created_at.timestamp())
    joined_ts = int(user.joined_at.timestamp()) if user.joined_at else None
    log.add_field(name="📅 Аккаунт создан", value=f"<t:{created_ts}:D> (<t:{created_ts}:R>)", inline=True)
    if joined_ts:
        log.add_field(name="🗓️ На сервере с", value=f"<t:{joined_ts}:D> (<t:{joined_ts}:R>)", inline=True)
    if fields:
        log.add_field(name="\u200b", value="**📝 Ответы из формы:**", inline=False)
        for label, value in fields:
            if value:
                log.add_field(name=label, value=value[:1024], inline=False)
    log.set_footer(text=f"Тикет ID: {channel.id}")
    await send_log(guild, log)

    await interaction.followup.send(f"✅ Тикет создан: {channel.mention}", ephemeral=True)


# ─── Modals ───────────────────────────────────────────────────────────────────

class BuilderModal(discord.ui.Modal, title="📐 Заявка — Билдер"):
    name_nick = discord.ui.TextInput(label="Ваше имя и ваш никнейм?", placeholder="Имя и ник", max_length=100)
    age = discord.ui.TextInput(label="Ваш возраст?", placeholder="От 15 лет", max_length=3)
    exp = discord.ui.TextInput(label="Опыт в строительстве", placeholder="Сколько месяцев/лет занимаетесь", style=discord.TextStyle.paragraph, max_length=500)
    examples = discord.ui.TextInput(label="Примеры ваших построек", placeholder="Ссылка на скрины", style=discord.TextStyle.paragraph, max_length=500)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        await create_ticket(i, "Заявка — Билдер", "📐",
            [("Имя и никнейм", self.name_nick.value), ("Возраст", self.age.value),
             ("Опыт в строительстве", self.exp.value), ("Примеры построек", self.examples.value)],
            "набор", "builder")


class MediaModal(discord.ui.Modal, title="🎬 Заявка — Медиа"):
    name_nick = discord.ui.TextInput(label="Ваше имя и ваш никнейм?", placeholder="Имя и ник", max_length=100)
    age = discord.ui.TextInput(label="Ваш возраст?", placeholder="От 15 лет", max_length=3)
    sphere = discord.ui.TextInput(label="В какой сфере вы снимаете?", placeholder="YouTube/Twitch/TikTok", max_length=100)
    channel_link = discord.ui.TextInput(label="Ваш канал?", placeholder="Укажите канал ссылкой", max_length=200)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        await create_ticket(i, "Заявка — Медиа", "🎬",
            [("Имя и никнейм", self.name_nick.value), ("Возраст", self.age.value),
             ("Сфера", self.sphere.value), ("Канал", self.channel_link.value)],
            "набор", "media")


class StaffModal(discord.ui.Modal, title="👮 Заявка — Стафф"):
    name_nick = discord.ui.TextInput(label="Ваше имя и ваш никнейм?", placeholder="Имя и ник", max_length=100)
    age = discord.ui.TextInput(label="Ваш возраст?", placeholder="От 15 лет", max_length=3)
    exp = discord.ui.TextInput(label="Опыт в стаффе?", placeholder="Ваш опыт и на каких проектах были", style=discord.TextStyle.paragraph, max_length=500)
    ratings = discord.ui.TextInput(label="Адекватность / Знание правил (0-10)", placeholder="Адекватность: X/10 | Правила: X/10", max_length=50)
    about = discord.ui.TextInput(label="Расскажите о себе?", style=discord.TextStyle.paragraph, max_length=500)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        await create_ticket(i, "Заявка — Стафф", "👮",
            [("Имя и никнейм", self.name_nick.value), ("Возраст", self.age.value),
             ("Опыт в стаффе", self.exp.value), ("Адекватность / Знание правил", self.ratings.value),
             ("О себе", self.about.value)],
            "набор", "staff_app")


class AppealModal(discord.ui.Modal, title="⚖️ Апелляция"):
    nickname = discord.ui.TextInput(label="Ваш никнейм", placeholder="Ник", max_length=100)
    punishment = discord.ui.TextInput(label="Тип наказания", placeholder="Бан/Мут/Кик", max_length=100)
    reason = discord.ui.TextInput(label="Почему наказание несправедливо?", placeholder="Объясни почему вам выдали его не по правилам", style=discord.TextStyle.paragraph, max_length=1000)
    proof = discord.ui.TextInput(label="Доказательство", placeholder="Ссылка на скрин/видео", max_length=500, required=False)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        await create_ticket(i, "Апелляция", "⚖️",
            [("Никнейм", self.nickname.value), ("Тип наказания", self.punishment.value),
             ("Почему несправедливо", self.reason.value), ("Доказательство", self.proof.value)],
            ticket_key="appeal")


class PlayerReportModal(discord.ui.Modal, title="⚠️ Жалоба на игрока"):
    nickname = discord.ui.TextInput(label="Ваш никнейм", placeholder="Ник", max_length=100)
    offender = discord.ui.TextInput(label="Ник нарушителя", placeholder="Ник", max_length=100)
    violation = discord.ui.TextInput(label="Что нарушил?", placeholder="Софт/Багаюз/Прочее", style=discord.TextStyle.paragraph, max_length=500)
    proof = discord.ui.TextInput(label="Доказательство", placeholder="Ссылка на скрин/видео", style=discord.TextStyle.paragraph, max_length=500)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        await create_ticket(i, "Жалоба на игрока", "⚠️",
            [("Ваш никнейм", self.nickname.value), ("Ник нарушителя", self.offender.value),
             ("Что нарушил", self.violation.value), ("Доказательство", self.proof.value)],
            ticket_key="player_report")


class StaffReportModal(discord.ui.Modal, title="📋 Жалоба на персонал"):
    nickname = discord.ui.TextInput(label="Ваш никнейм", placeholder="Ник", max_length=100)
    staff_nick = discord.ui.TextInput(label="Ник персонала", placeholder="Ник", max_length=100)
    violation = discord.ui.TextInput(label="Что нарушил?", placeholder="Угрожал/Материл/Прочее", style=discord.TextStyle.paragraph, max_length=500)
    proof = discord.ui.TextInput(label="Доказательство", placeholder="Ссылка на скрин/видео", max_length=500)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        await create_ticket(i, "Жалоба на персонал", "📋",
            [("Ваш никнейм", self.nickname.value), ("Ник персонала", self.staff_nick.value),
             ("Что нарушил", self.violation.value), ("Доказательство", self.proof.value)],
            ticket_key="staff_report")


class GeneralModal(discord.ui.Modal, title="❓ Общий вопрос"):
    question = discord.ui.TextInput(label="Вопрос", style=discord.TextStyle.paragraph, max_length=1000)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        await create_ticket(i, "Общий вопрос", "❓", [("Вопрос", self.question.value)],
            ticket_key="general")


# ─── Persistent Views ─────────────────────────────────────────────────────────

class StaffPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="📐 Билдер", style=discord.ButtonStyle.primary, custom_id="sp_builder")
    async def builder(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(BuilderModal())

    @discord.ui.button(label="🎬 Медиа", style=discord.ButtonStyle.primary, custom_id="sp_media")
    async def media(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(MediaModal())

    @discord.ui.button(label="👮 Стафф", style=discord.ButtonStyle.primary, custom_id="sp_staff")
    async def staff(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(StaffModal())


class SupportPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚖️ Апелляция", style=discord.ButtonStyle.secondary, custom_id="sup_appeal", row=0)
    async def appeal(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(AppealModal())

    @discord.ui.button(label="⚠️ Жалоба на игрока", style=discord.ButtonStyle.secondary, custom_id="sup_player", row=0)
    async def player(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(PlayerReportModal())

    @discord.ui.button(label="📋 Жалоба на персонал", style=discord.ButtonStyle.secondary, custom_id="sup_staff_rep", row=1)
    async def staff_rep(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(StaffReportModal())

    @discord.ui.button(label="❓ Общие вопросы", style=discord.ButtonStyle.secondary, custom_id="sup_general", row=1)
    async def general(self, i: discord.Interaction, b: discord.ui.Button):
        await i.response.send_modal(GeneralModal())


class CloseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Закрыть тикет", style=discord.ButtonStyle.danger, custom_id="close_ticket")
    async def close(self, i: discord.Interaction, b: discord.ui.Button):
        user = i.user
        channel = i.channel
        embed = discord.Embed(
            title="🔒 Тикет закрыт",
            description=f"Закрыт пользователем {user.mention}. Канал удалится через 5 сек.",
            color=discord.Color.red(), timestamp=datetime.now()
        )
        await i.response.send_message(embed=embed)

        closed_at = datetime.now()
        td = ticket_data.pop(channel.id, None)

        log = discord.Embed(title="🔒 Тикет закрыт", color=discord.Color.red(), timestamp=closed_at)
        log.set_author(name=str(user), icon_url=user.display_avatar.url)
        log.set_thumbnail(url=user.display_avatar.url)
        log.add_field(name="🔐 Закрыл", value=f"{user.mention}\n`{user}` (ID: `{user.id}`)", inline=True)
        log.add_field(name="📌 Канал", value=f"`{channel.name}`", inline=True)
        if td:
            opener = td["user"]
            log.add_field(name="📁 Категория", value=td["category"], inline=True)
            log.add_field(name="👤 Открыл", value=f"{opener.mention}\n`{opener}`", inline=True)
            duration = closed_at - td["opened_at"]
            total = int(duration.total_seconds())
            h, rem = divmod(total, 3600)
            m, s = divmod(rem, 60)
            dur_str = (f"{h}ч " if h else "") + (f"{m}м " if m else "") + f"{s}с"
            log.add_field(name="⏱️ Длительность", value=dur_str, inline=True)
            self_close = opener.id == user.id
            log.add_field(name="ℹ️ Закрыл сам?", value="Да" if self_close else "Нет (стафф)", inline=True)
        log.set_footer(text=f"Канал ID: {channel.id}")
        await send_log(i.guild, log)

        await asyncio.sleep(5)
        await channel.delete(reason=f"Тикет закрыт {user}")


# ─── Slash-команды ────────────────────────────────────────────────────────────

@bot.tree.command(name="staff", description="Панель набора персонала")
@app_commands.checks.has_permissions(administrator=True)
async def slash_staff(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    try:
        embed = discord.Embed(
            title="👥 Набор на персонал",
            description=(
                "Хочешь стать частью команды?\n"
                "Выбери должность и заполни заявку.\n\n"
                "**📐 Билдер** — строительство и оформление\n"
                "**🎬 Медиа** — контент, дизайн, видео\n"
                "**👮 Стафф** — модерация и управление"
            ),
            color=discord.Color.from_rgb(30, 100, 220)
        )
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.set_footer(text=interaction.guild.name)
        await interaction.followup.send(embed=embed, view=StaffPanel())
    except Exception as e:
        print(f"[/staff] Ошибка: {e}")
        await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


@bot.tree.command(name="support", description="Панель поддержки")
@app_commands.checks.has_permissions(administrator=True)
async def slash_support(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)
    try:
        embed = discord.Embed(
            title="🎫 Служба поддержки",
            description=(
                "Нужна помощь? Выбери категорию и открой тикет.\n\n"
                "**⚖️ Апелляция** — обжаловать наказание\n"
                "**⚠️ Жалоба на игрока** — сообщить о нарушителе\n"
                "**📋 Жалоба на персонал** — жалоба на сотрудника\n"
                "**❓ Общие вопросы** — любые другие вопросы"
            ),
            color=discord.Color.from_rgb(30, 100, 220)
        )
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)
        embed.set_footer(text=interaction.guild.name)
        await interaction.followup.send(embed=embed, view=SupportPanel())
    except Exception as e:
        print(f"[/support] Ошибка: {e}")
        await interaction.followup.send(f"❌ Ошибка: {e}", ephemeral=True)


@bot.tree.command(name="logs", description="Установить канал для логов")
@app_commands.checks.has_permissions(administrator=True)
async def slash_logs(interaction: discord.Interaction, channel: discord.TextChannel):
    _cfg.setdefault(str(interaction.guild.id), {})["log_channel_id"] = channel.id
    save_cfg()
    await interaction.response.send_message(f"✅ Лог-канал: {channel.mention}", ephemeral=True)


@bot.tree.command(name="close", description="Закрыть тикет")
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_close(interaction: discord.Interaction):
    user = interaction.user
    channel = interaction.channel
    embed = discord.Embed(
        title="🔒 Тикет закрыт",
        description=f"Закрыт {user.mention}. Канал удалится через 5 сек.",
        color=discord.Color.red(), timestamp=datetime.now()
    )
    await interaction.response.send_message(embed=embed)

    closed_at = datetime.now()
    td = ticket_data.pop(channel.id, None)

    log = discord.Embed(title="🔒 Тикет закрыт", color=discord.Color.red(), timestamp=closed_at)
    log.set_author(name=str(user), icon_url=user.display_avatar.url)
    log.set_thumbnail(url=user.display_avatar.url)
    log.add_field(name="🔐 Закрыл", value=f"{user.mention}\n`{user}` (ID: `{user.id}`)", inline=True)
    log.add_field(name="📌 Канал", value=f"`{channel.name}`", inline=True)
    if td:
        opener = td["user"]
        log.add_field(name="📁 Категория", value=td["category"], inline=True)
        log.add_field(name="👤 Открыл", value=f"{opener.mention}\n`{opener}`", inline=True)
        duration = closed_at - td["opened_at"]
        total = int(duration.total_seconds())
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        dur_str = (f"{h}ч " if h else "") + (f"{m}м " if m else "") + f"{s}с"
        log.add_field(name="⏱️ Длительность", value=dur_str, inline=True)
        self_close = opener.id == user.id
        log.add_field(name="ℹ️ Закрыл сам?", value="Да" if self_close else "Нет (стафф)", inline=True)
    log.set_footer(text=f"Канал ID: {channel.id}")
    await send_log(interaction.guild, log)
    await asyncio.sleep(5)
    await channel.delete()


TICKET_NAMES = {
    "appeal": "⚖️ Апелляция",
    "player_report": "⚠️ Жалоба на игрока",
    "staff_report": "📋 Жалоба на персонал",
    "general": "❓ Общие вопросы",
    "builder": "📐 Билдер",
    "media": "🎬 Медиа",
    "staff_app": "👮 Стафф",
}


class RoleSelectView(discord.ui.View):
    def __init__(self, ticket_key: str, ticket_name: str, guild_id: int):
        super().__init__(timeout=180)
        self.ticket_key = ticket_key
        self.ticket_name = ticket_name
        self.guild_id = guild_id
        self.chosen: list[discord.Role] = []

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Выбери роли (можно несколько)...",
        min_values=0,
        max_values=25,
    )
    async def role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        self.chosen = list(select.values)
        role_text = " ".join(r.mention for r in self.chosen) if self.chosen else "_(нет)_"
        await interaction.response.edit_message(
            content=f"**{self.ticket_name}** — выбрано: {role_text}\nНажми **Сохранить**.",
            view=self,
        )

    @discord.ui.button(label="✅ Сохранить", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_cfg = _cfg.setdefault(str(self.guild_id), {})
        guild_cfg.setdefault("ticket_roles", {})[self.ticket_key] = [r.id for r in self.chosen]
        save_cfg()
        role_text = " ".join(r.mention for r in self.chosen) if self.chosen else "_(нет)_"
        await interaction.response.edit_message(
            content=f"✅ **{self.ticket_name}** — роли сохранены: {role_text}",
            view=None,
        )

    @discord.ui.button(label="❌ Отмена", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Отменено.", view=None)


@bot.tree.command(name="ticket-roles", description="Настроить роли для типа тикета")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(ticket=[
    app_commands.Choice(name=v, value=k) for k, v in TICKET_NAMES.items()
])
async def slash_ticket_roles(interaction: discord.Interaction, ticket: str):
    name = TICKET_NAMES.get(ticket, ticket)
    view = RoleSelectView(ticket, name, interaction.guild.id)
    await interaction.response.send_message(
        f"Выбери роли для **{name}**:", view=view, ephemeral=True
    )


@bot.tree.command(name="ticket-roles-clear", description="Сбросить роли для типа тикета")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.choices(ticket=[
    app_commands.Choice(name=v, value=k) for k, v in TICKET_NAMES.items()
])
async def slash_ticket_roles_clear(interaction: discord.Interaction, ticket: str):
    guild_cfg = _cfg.setdefault(str(interaction.guild.id), {})
    guild_cfg.setdefault("ticket_roles", {}).pop(ticket, None)
    save_cfg()
    name = TICKET_NAMES.get(ticket, ticket)
    await interaction.response.send_message(
        f"🗑️ Роли для **{name}** сброшены — теперь при создании тикета роли не упоминаются.", ephemeral=True
    )


@bot.tree.command(name="add-user", description="Добавить пользователя в тикет")
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_add_user(interaction: discord.Interaction, user: discord.Member):
    try:
        channel = interaction.channel
        await channel.set_permissions(
            user,
            read_messages=True,
            send_messages=True,
            attach_files=True,
            embed_links=True,
        )
        await interaction.response.send_message(
            f"✅ {user.mention} добавлен в тикет.", ephemeral=True
        )
        embed = discord.Embed(
            description=f"➕ {interaction.user.mention} добавил {user.mention} в тикет.",
            color=discord.Color.blurple(),
        )
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[/add-user] Ошибка: {e}")
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


@bot.tree.command(name="remove-user", description="Убрать пользователя из тикета")
@app_commands.checks.has_permissions(manage_channels=True)
async def slash_remove_user(interaction: discord.Interaction, user: discord.Member):
    try:
        channel = interaction.channel
        await channel.set_permissions(user, overwrite=None)
        await interaction.response.send_message(
            f"✅ {user.mention} убран из тикета.", ephemeral=True
        )
        embed = discord.Embed(
            description=f"➖ {interaction.user.mention} убрал {user.mention} из тикета.",
            color=discord.Color.orange(),
        )
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[/remove-user] Ошибка: {e}")
        if not interaction.response.is_done():
            await interaction.response.send_message(f"❌ Ошибка: {e}", ephemeral=True)


@bot.tree.error
async def on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"[TREE ERROR] {interaction.command and interaction.command.name}: {error}")
    msg = "❌ Нет прав." if isinstance(error, app_commands.MissingPermissions) else f"❌ Ошибка: {error}"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


# ─── on_ready ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    load_cfg()
    bot.add_view(StaffPanel())
    bot.add_view(SupportPanel())
    bot.add_view(CloseView())

    # Аватар — только один раз (если логотип есть)
    if LOGO_BYTES and not _cfg.get("avatar_set"):
        try:
            await bot.user.edit(avatar=LOGO_BYTES)
            _cfg["avatar_set"] = True
            save_cfg()
            print("Аватар обновлён")
        except Exception as e:
            print(f"Аватар: {e}")

    # Синхронизация команд — только один раз
    if not _cfg.get("commands_synced"):
        try:
            total = 0
            for guild in bot.guilds:
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                total += len(synced)
            _cfg["commands_synced"] = True
            save_cfg()
            print(f"Команды синхронизированы: {total}")
        except Exception as e:
            print(f"Синхронизация: {e}")
    else:
        print("Команды уже синхронизированы, пропускаем")

    print(f"✅ Бот запущен: {bot.user} (ID: {bot.user.id}), серверов: {len(bot.guilds)}")


if __name__ == "__main__":
    DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN environment variable is not set. Please set it in Railway.")
        exit(1)
    start_keepalive()
    bot.run(DISCORD_TOKEN)
