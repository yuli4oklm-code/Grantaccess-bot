"""
Bot Discord : commande Stripe + livraison automatique des weights via l'API Winsight.
+ /weight system avec specs, tickets, et order flow
+ multi-serveur (config par guild via /setup)
"""

import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import json
import threading
import time
import re
from urllib.parse import urlparse, quote

import stripe
from flask import Flask, request, jsonify
import aiohttp

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]

STRIPE_SECRET_KEY = os.environ["STRIPE_SECRET_KEY"]
STRIPE_WEBHOOK_SECRET = os.environ["STRIPE_WEBHOOK_SECRET"]
stripe.api_key = STRIPE_SECRET_KEY

WINSIGHT_USERNAME = os.environ.get("WINSIGHT_USERNAME", "")
WINSIGHT_PASSWORD = os.environ.get("WINSIGHT_PASSWORD", "")
WINSIGHT_URL = os.environ.get("WINSIGHT_URL", "https://winsight.info/px3-8kd4b7e2a1f9")

HELIOS_API_KEY = os.environ.get("HELIOS_API_KEY", "")
HELIOS_RESOURCE_ID = os.environ.get("HELIOS_RESOURCE_ID", "6cabf1d2-8887-4b36-885d-e888b82f7987")
HELIOS_API_URL = "https://www.inputsense.com/api/scripts/integration_v1.php"

# Valeurs par défaut (serveur principal). Chaque serveur peut les surcharger avec /setup.
STAFF_CHANNEL_ID = int(os.environ.get("STAFF_CHANNEL_ID", "0")) or None
ORDER_PANEL_CHANNEL_ID = int(os.environ.get("ORDER_PANEL_CHANNEL_ID", "0")) or None
VOUCH_CHANNEL_ID = int(os.environ.get("VOUCH_CHANNEL_ID", "0")) or None
TICKET_CATEGORY_ID = int(os.environ.get("TICKET_CATEGORY_ID", "1513173071916302499")) or None

# Le backup de config est stocké dans ce salon (global, pas par serveur).
BACKUP_CHANNEL_ID = int(os.environ.get("BACKUP_CHANNEL_ID", "0")) or STAFF_CHANNEL_ID

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
ORDERS_FILE = os.path.join(DATA_DIR, "winsight_orders.json")
PRODUCTS_FILE = os.path.join(DATA_DIR, "winsight_products.json")
PIPELINES_FILE = os.path.join(DATA_DIR, "winsight_pipelines.json")
WEIGHTS_FILE = os.path.join(DATA_DIR, "weights.json")
GUILDS_FILE = os.path.join(DATA_DIR, "guilds.json")
HELIOS_RESOURCES_FILE = os.path.join(DATA_DIR, "helios_resources.json")

EMBED_COLOR = 0x2F3136
CONFIG_BACKUP_MARKER = "WINSIGHT_BOT_CONFIG_BACKUP_V1"

DEFAULT_PRODUCTS = {
    "XyCubValorantV2": {"winsight": 2000},
}
DEFAULT_PIPELINES = {}

# ─────────────────────────────────────────────
#  PLATEFORMES
# ─────────────────────────────────────────────
#  La cle est l'identifiant interne (utilise dans products.json et le code) ;
#  le label est ce que voient les clients. Helios est le backend derriere
#  Aim Engine et Cubism.

PLATFORMS = {
    "winsight": {"label": "Winsight", "emoji": "\U0001F451"},
    "helios": {"label": "Aim Engine \u00b7 Cubism", "emoji": "\U0001F3AF"},
}


def platform_label(key: str) -> str:
    return PLATFORMS.get(key, {}).get("label", str(key).capitalize())

# ─────────────────────────────────────────────
#  MAPPING NOM MODELE → DISPLAY NAME
# ─────────────────────────────────────────────

def strip_prefix(model_name: str) -> str:
    """Retire les préfixes connus (XyCub, Moon, etc.)."""
    prefixes = ["XyCub", "xycub", "Moon", "moon", "MOON"]
    for prefix in prefixes:
        if model_name.startswith(prefix):
            return model_name[len(prefix):]
    return model_name


def get_display_name(model_name: str) -> str:
    """
    Transforme XyCubValorantV2 → Valorant V2, XyCubValorantV3 → Valorant V3, etc.
    """
    name = strip_prefix(model_name)
    # CamelCase → mots séparés
    name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
    name = re.sub(r'(?<=[a-zA-Z])(V\d+)', r' \1', name)
    return name.strip()


def get_game_group(model_name: str) -> str:
    """
    Retourne le nom du 'jeu' sans numéro de version.
    XyCubValorantV2 → "Valorant"
    XyCubValorantV3 → "Valorant"
    XyCubCODV1     → "COD"
    Permet de regrouper toutes les versions d'un même jeu.
    """
    name = strip_prefix(model_name)
    # CamelCase → mots
    name = re.sub(r'(?<=[a-z])(?=[A-Z])', ' ', name)
    # Retirer le suffixe Vx (version)
    name = re.sub(r'\s*V\d+\s*$', '', name).strip()
    return name


def get_models_for_group(group_name: str) -> list[str]:
    """
    Retourne tous les model_names dont le game_group correspond à group_name (insensible à la casse).
    Ex: "Valorant" → ["XyCubValorantV2", "XyCubValorantV3"]
    """
    products = load_products()
    return [
        name for name in products.keys()
        if get_game_group(name).lower() == group_name.lower()
    ]


def get_grouped_products() -> dict[str, list[str]]:
    """
    Retourne un dict {group_name: [model1, model2, ...]} groupé par jeu.
    Ex: {"Valorant": ["XyCubValorantV2", "XyCubValorantV3"], "COD": [...]}
    """
    products = load_products()
    groups: dict[str, list[str]] = {}
    for name in products.keys():
        group = get_game_group(name)
        groups.setdefault(group, []).append(name)
    return groups


# ─────────────────────────────────────────────
#  STOCKAGE
# ─────────────────────────────────────────────

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_orders():
    return load_json(ORDERS_FILE, {})


def save_orders(data):
    save_json(ORDERS_FILE, data)


def load_products():
    data = load_json(PRODUCTS_FILE, DEFAULT_PRODUCTS)
    normalized = {}
    for name, value in data.items():
        if isinstance(value, dict) and "price" in value and "platform" in value:
            normalized[name] = {value["platform"]: value["price"]}
        elif isinstance(value, dict):
            normalized[name] = value
        else:
            normalized[name] = {"winsight": value}
    return normalized


def load_pipelines():
    return load_json(PIPELINES_FILE, DEFAULT_PIPELINES)


def save_pipelines(data):
    save_json(PIPELINES_FILE, data)


def load_helios_resources():
    return load_json(HELIOS_RESOURCES_FILE, {})


def save_helios_resources(data):
    save_json(HELIOS_RESOURCES_FILE, data)


def get_helios_resource(model_name: str):
    """
    Resource ID Helios d'un modèle. Retombe sur HELIOS_RESOURCE_ID (ressource
    globale historique) si aucun mapping n'existe, pour ne pas casser les
    modèles déjà en place.
    """
    if not model_name:
        return HELIOS_RESOURCE_ID or None
    resources = load_helios_resources()
    if model_name in resources:
        return resources[model_name]
    target = normalize_weight_name(model_name)
    for name, rid in resources.items():
        if normalize_weight_name(name) == target:
            return rid
    return HELIOS_RESOURCE_ID or None


def get_models_on_platform(group_name: str, platform: str) -> list[str]:
    """Modèles d'un groupe (ex: « Valorant ») disponibles sur une plateforme."""
    products = load_products()
    return [m for m in get_models_for_group(group_name) if platform in products.get(m, {})]

def load_weights():
    return load_json(WEIGHTS_FILE, {})


def save_weights(data):
    save_json(WEIGHTS_FILE, data)


# ─────────────────────────────────────────────
#  CONFIG PAR SERVEUR (multi-guild)
# ─────────────────────────────────────────────

# Clés de config par serveur → valeur par défaut (env var du serveur principal).
GUILD_CONFIG_DEFAULTS = {
    "staff_channel_id": STAFF_CHANNEL_ID,
    "order_panel_channel_id": ORDER_PANEL_CHANNEL_ID,
    "vouch_channel_id": VOUCH_CHANNEL_ID,
    "ticket_category_id": TICKET_CATEGORY_ID,
}


def load_guilds():
    return load_json(GUILDS_FILE, {})


def save_guilds(data):
    save_json(GUILDS_FILE, data)


def get_guild_config(guild_id) -> dict:
    """
    Retourne la config d'un serveur. Les clés non définies retombent sur les
    valeurs par défaut (env vars) — pratique pour le serveur principal qui
    n'a rien à configurer.
    """
    stored = load_guilds().get(str(guild_id), {}) if guild_id else {}
    config = dict(GUILD_CONFIG_DEFAULTS)
    for key, value in stored.items():
        if key in config:
            config[key] = value or None
    return config


def set_guild_config(guild_id, **kwargs):
    """Enregistre une ou plusieurs clés de config pour un serveur."""
    data = load_guilds()
    entry = data.setdefault(str(guild_id), {})
    for key, value in kwargs.items():
        if key not in GUILD_CONFIG_DEFAULTS:
            continue
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    save_guilds(data)


def get_guild_channel(guild_id, key):
    """
    Récupère un salon Discord depuis la config du serveur (ou None).

    On vérifie que le salon appartient bien au serveur demandé : sinon un
    second serveur non configuré hériterait des salons du serveur principal
    (via les env vars par défaut) et enverrait ses tickets/avis chez nous.
    """
    channel_id = get_guild_config(guild_id).get(key)
    if not channel_id:
        return None
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return None
    guild = getattr(channel, "guild", None)
    if guild_id and guild and guild.id != int(guild_id):
        return None
    return channel


def create_order(order_id, buyer_id, buyer_contact, model, platform, stripe_session_id, guild_id=None):
    data = load_orders()
    data[order_id] = {
        "buyer_id": buyer_id,
        "buyer_contact": buyer_contact,
        "model": model,
        "platform": platform,
        "stripe_session_id": stripe_session_id,
        "guild_id": guild_id,
        "status": "pending_payment",
        "staff_message_id": None,
        "created_at": time.time(),
    }
    save_orders(data)


def get_order(order_id):
    return load_orders().get(order_id)


def get_order_by_session(session_id):
    data = load_orders()
    for oid, o in data.items():
        if o.get("stripe_session_id") == session_id:
            return oid, o
    return None, None


def update_order(order_id, **kwargs):
    data = load_orders()
    if order_id in data:
        data[order_id].update(kwargs)
        save_orders(data)


# ─────────────────────────────────────────────
#  BOT DISCORD
# ─────────────────────────────────────────────

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


@tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command. Admin only.",
            ephemeral=True,
        )
    else:
        print(f"[Command Error] {error}")
        try:
            await interaction.response.send_message(
                "❌ An unexpected error occurred while running this command.",
                ephemeral=True,
            )
        except discord.InteractionResponded:
            pass


@bot.event
async def on_error(event_method, *args, **kwargs):
    import traceback
    print(f"[Bot Error] in {event_method}: {traceback.format_exc()}")

main_loop = None


async def get_config_backup_channel():
    if not BACKUP_CHANNEL_ID:
        return None
    channel = bot.get_channel(BACKUP_CHANNEL_ID)
    if channel:
        return channel
    try:
        return await bot.fetch_channel(BACKUP_CHANNEL_ID)
    except Exception as e:
        print(f"[Config Backup] Could not fetch staff channel: {e}")
        return None


async def find_config_backup_message():
    channel = await get_config_backup_channel()
    if not channel:
        return None
    try:
        async for message in channel.history(limit=100):
            if message.author == bot.user and CONFIG_BACKUP_MARKER in message.content:
                return message
    except Exception as e:
        print(f"[Config Backup] Could not read channel history: {e}")
    return None


def parse_config_backup(content):
    start = content.find("{")
    end = content.rfind("}") + 1
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(content[start:end])
    except json.JSONDecodeError as e:
        print(f"[Config Backup] Invalid backup JSON: {e}")
        return None


async def restore_config_from_discord():
    message = await find_config_backup_message()
    if not message:
        print("[Config Backup] No Discord backup found.")
        return False
    payload = parse_config_backup(message.content)
    if not payload:
        return False
    if "products" in payload:
        save_json(PRODUCTS_FILE, payload["products"])
    if "pipelines" in payload:
        save_pipelines(payload["pipelines"])
    if "helios_resources" in payload:
        save_helios_resources(payload["helios_resources"])
    print("[Config Backup] Restored products/pipelines from Discord backup.")
    return True


async def backup_config_to_discord(reason="manual update"):
    channel = await get_config_backup_channel()
    if not channel:
        print("[Config Backup] BACKUP_CHANNEL_ID/STAFF_CHANNEL_ID is not configured; backup skipped.")
        return
    payload = {
        "version": 1,
        "updated_at": int(time.time()),
        "reason": reason,
        "products": load_products(),
        "pipelines": load_pipelines(),
        "helios_resources": load_helios_resources(),
    }
    content = f"{CONFIG_BACKUP_MARKER}\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    if len(content) > 2000:
        print("[Config Backup] Backup is too large for one Discord message; backup skipped.")
        return
    try:
        message = await find_config_backup_message()
        if message:
            await message.edit(content=content)
            print(f"[Config Backup] Updated Discord backup ({reason}).")
        else:
            await channel.send(content)
            print(f"[Config Backup] Created Discord backup ({reason}).")
    except Exception as e:
        print(f"[Config Backup] Could not write Discord backup: {e}")


ORDER_EMBED_TITLE = "🛒 Order a Weight"
ORDER_EMBED_DESCRIPTION = (
    "Click the button below to order. You'll enter your contact info and the model "
    "you want, then pay securely via Stripe. Delivery is automatic after payment."
)


# ─────────────────────────────────────────────
#  WEIGHT SYSTEM
# ─────────────────────────────────────────────

def build_weight_embed(weight_data: dict) -> discord.Embed:
    """Construit l'embed d'affichage d'un weight — style propre proche du screenshot Rankzilla."""
    name        = weight_data.get("name", "Unknown")
    description = weight_data.get("description", "")
    game        = weight_data.get("game", "")
    price_display = weight_data.get("price_display", "")
    platforms   = weight_data.get("platforms", [])
    version     = weight_data.get("version", "")
    author      = weight_data.get("author", "")
    image_url   = weight_data.get("image_url", "")

    embed = discord.Embed(title=name, description=description or None, color=0x5865F2)

    # Game + Platforms sur la même ligne (deux inline fields)
    if game:
        embed.add_field(name="🎮 Game", value=game, inline=True)

    if platforms:
        platform_str = " • ".join(p.upper() for p in platforms)
        embed.add_field(name="🖥️ Platforms", value=platform_str, inline=True)

    # Saut de ligne visuel si on a deux colonnes
    if game and platforms:
        embed.add_field(name="\u200b", value="\u200b", inline=False)

    # Prix + Includes sur la même ligne
    if price_display:
        embed.add_field(name="💰 Price", value=price_display, inline=True)

    embed.add_field(name="📦 Includes", value="• All current and future versions", inline=True)

    if version or author:
        footer_parts = []
        if version:
            footer_parts.append(version)
        if author:
            footer_parts.append(f"by @{author}")
        embed.set_footer(text=" • ".join(footer_parts))

    if image_url:
        embed.set_image(url=image_url)

    return embed


def build_specs_embed(weight_data: dict) -> discord.Embed:
    """Construit l'embed specs (style screenshot avec sections General/AE/WIN)."""
    name = weight_data.get("name", "Unknown")
    specs_general = weight_data.get("specs_general", "")
    specs_ae = weight_data.get("specs_ae", "")
    specs_win = weight_data.get("specs_win", "")
    version = weight_data.get("version", "")
    author = weight_data.get("author", "")

    embed = discord.Embed(
        title=f"🗂️ {name} — Specs",
        color=0x2F3136,
    )

    if specs_general:
        embed.add_field(name="🌐 General", value=specs_general, inline=False)
    if specs_ae:
        embed.add_field(name="AE", value=specs_ae, inline=False)
    if specs_win:
        embed.add_field(name="WIN", value=specs_win, inline=False)

    if version or author:
        footer_parts = []
        if version:
            footer_parts.append(version)
        if author:
            footer_parts.append(f"by @{author}")
        embed.set_footer(text=" • ".join(footer_parts))

    return embed


class WeightView(discord.ui.View):
    """View persistante avec les boutons Specs, Purchase Ticket, Order Weight."""

    def __init__(self, weight_id: str):
        super().__init__(timeout=None)
        self.weight_id = weight_id
        # On encode le weight_id dans le custom_id pour persistance
        self.children[0].custom_id = f"weight_specs_{weight_id}"
        self.children[1].custom_id = f"weight_ticket_{weight_id}"
        self.children[2].custom_id = f"weight_order_{weight_id}"

    @discord.ui.button(label="📋 Specs", style=discord.ButtonStyle.secondary, custom_id="weight_specs_placeholder")
    async def specs_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        weight_id = button.custom_id.replace("weight_specs_", "")
        weights = load_weights()
        weight_data = weights.get(weight_id)
        if not weight_data:
            await interaction.response.send_message("❌ Weight not found.", ephemeral=True)
            return
        embed = build_specs_embed(weight_data)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="🎫 Purchase Ticket", style=discord.ButtonStyle.primary, custom_id="weight_ticket_placeholder")
    async def ticket_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        weight_id = button.custom_id.replace("weight_ticket_", "")
        weights = load_weights()
        weight_data = weights.get(weight_id)
        if not weight_data:
            await interaction.response.send_message("❌ Weight not found.", ephemeral=True)
            return

        guild = interaction.guild
        config = get_guild_config(guild.id)
        category = None
        if config.get("ticket_category_id"):
            category = guild.get_channel(int(config["ticket_category_id"]))

        # Vérifier si l'utilisateur a déjà un ticket ouvert pour CE weight
        user_slug = re.sub(r'[^a-z0-9\-]', '-', interaction.user.name.lower())
        weight_slug = re.sub(r'[^a-z0-9\-]', '-', weight_data.get("name", "weight").lower())
        ticket_name = f"ticket-{user_slug}-{weight_slug}"[:100]
        existing = discord.utils.get(guild.text_channels, name=ticket_name)
        if existing:
            await interaction.response.send_message(
                f"❌ You already have an open ticket: {existing.mention}", ephemeral=True
            )
            return

        # Créer le salon ticket
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        # Donner accès aux admins
        for role in guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        try:
            ticket_channel = await guild.create_text_channel(
                name=ticket_name,
                category=category,
                overwrites=overwrites,
                topic=f"Purchase ticket for {weight_data['name']} | {interaction.user.id}",
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Could not create ticket: {e}", ephemeral=True)
            return

        # Embed dans le ticket
        weight_name = weight_data.get("name", "Unknown")
        price_display = weight_data.get("price_display", "")

        ticket_embed = discord.Embed(
            title=f"🎫 Purchase Ticket — {weight_name}",
            description=(
                f"Hello {interaction.user.mention}! 👋\n\n"
                f"You've opened a ticket to purchase **{weight_name}**.\n"
                f"{'**Price:** ' + price_display + chr(10) if price_display else ''}"
                f"\nA staff member will assist you shortly.\n\n"
                f"*You can also use the button below to order directly via Stripe.*"
            ),
            color=0x5865F2,
        )
        ticket_embed.set_footer(text=f"Ticket opened by {interaction.user} • {interaction.user.id}")

        # Bouton de fermeture du ticket
        close_view = TicketCloseView(interaction.user.id)
        await ticket_channel.send(
            content=f"{interaction.user.mention}",
            embed=ticket_embed,
            view=close_view,
        )

        # Ping staff si configuré
        staff_channel = get_guild_channel(guild.id, "staff_channel_id")
        if staff_channel:
            await staff_channel.send(
                f"📬 New purchase ticket for **{weight_name}** by {interaction.user.mention} → {ticket_channel.mention}"
            )

        await interaction.response.send_message(
            f"✅ Your ticket has been created: {ticket_channel.mention}", ephemeral=True
        )

    @discord.ui.button(label="💳 Order Weight", style=discord.ButtonStyle.success, custom_id="weight_order_placeholder")
    async def order_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        weight_id = button.custom_id.replace("weight_order_", "")
        weights = load_weights()
        weight_data = weights.get(weight_id)
        if not weight_data:
            await interaction.response.send_message("❌ Weight not found.", ephemeral=True)
            return

        # Trouver le modèle correspondant dans les produits
        model_name = weight_data.get("product_model", "")
        products = load_products()

        if not model_name or model_name not in products:
            # Fallback: afficher le sélecteur général
            select_view = discord.ui.View(timeout=120)
            select_view.add_item(ModelSelect())
            await interaction.response.send_message(
                "Which model would you like to order?", view=select_view, ephemeral=True
            )
            return

        available_platforms = products[model_name]
        if len(available_platforms) == 1:
            only_platform = next(iter(available_platforms))
            await interaction.response.send_modal(ContactOnlyModal(model_name, only_platform))
        else:
            platform_view = discord.ui.View(timeout=120)
            platform_view.add_item(PlatformSelect(model_name, available_platforms))
            await interaction.response.send_message(
                f"**{get_display_name(model_name)}** is available on multiple platforms. Which one?",
                view=platform_view,
                ephemeral=True,
            )


class TicketCloseView(discord.ui.View):
    """View avec bouton de fermeture du ticket."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=None)
        self.owner_id = owner_id

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Seul le staff (admin) ou l'owner peut fermer
        is_admin = interaction.user.guild_permissions.administrator
        is_owner = interaction.user.id == self.owner_id

        if not is_admin and not is_owner:
            await interaction.response.send_message("❌ You can't close this ticket.", ephemeral=True)
            return

        await interaction.response.send_message("🔒 Closing ticket in 5 seconds...")
        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except Exception as e:
            print(f"[Ticket] Could not delete channel: {e}")


# ─────────────────────────────────────────────
#  WEIGHT MODAL
# ─────────────────────────────────────────────

class WeightAddModal(discord.ui.Modal, title="Add / Edit Weight"):

    def __init__(self, weight_id: str = None, existing: dict = None):
        super().__init__()
        self.weight_id = weight_id

        # Reconstruire le champ multi-info si édition
        image_default = ""
        if existing:
            parts = []
            if existing.get("image_url"):
                parts.append(f"image: {existing['image_url']}")
            if existing.get("platforms"):
                parts.append(f"platforms: {','.join(existing['platforms'])}")
            if existing.get("version"):
                parts.append(f"version: {existing['version']}")
            if existing.get("author"):
                parts.append(f"author: {existing['author']}")
            if existing.get("product_model"):
                parts.append(f"model: {existing['product_model']}")
            image_default = "\n".join(parts)

        self.name_input = discord.ui.TextInput(
            label="Weight name (ex: Rankzilla)",
            max_length=100,
            required=True,
            placeholder="Rankzilla",
            default=existing.get("name", "") if existing else "",
        )
        self.description_input = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            max_length=300,
            required=True,
            placeholder="Pure ranked dataset, optimized for CDL maps...",
            default=existing.get("description", "") if existing else "",
        )
        self.game_input = discord.ui.TextInput(
            label="Game",
            max_length=100,
            required=False,
            placeholder="Call of Duty: Black Ops 7",
            default=existing.get("game", "") if existing else "",
        )
        self.price_input = discord.ui.TextInput(
            label="Price display (ex: $30 USD)",
            max_length=100,
            required=False,
            placeholder="$30 USD",
            default=existing.get("price_display", "") if existing else "",
        )
        self.image_input = discord.ui.TextInput(
            label="Image URL + platforms + version + author",
            style=discord.TextStyle.paragraph,
            max_length=800,
            required=False,
            placeholder="image: https://...\nplatforms: ae,win\nversion: v1.46.3\nauthor: swt\nmodel: XyCubValorantV2",
            default=image_default,
        )

        self.add_item(self.name_input)
        self.add_item(self.description_input)
        self.add_item(self.game_input)
        self.add_item(self.price_input)
        self.add_item(self.image_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Parser le champ multi-info
        image_url = ""
        platforms = []
        version = ""
        author = ""
        product_model = ""

        for line in self.image_input.value.strip().splitlines():
            line = line.strip()
            if line.lower().startswith("image:"):
                image_url = line[6:].strip()
            elif line.lower().startswith("platforms:"):
                raw = line[10:].strip()
                platforms = [p.strip().lower() for p in raw.split(",") if p.strip()]
            elif line.lower().startswith("version:"):
                version = line[8:].strip()
            elif line.lower().startswith("author:"):
                author = line[7:].strip()
            elif line.lower().startswith("model:"):
                product_model = line[6:].strip()

        weight_data = {
            "name": self.name_input.value.strip(),
            "description": self.description_input.value.strip(),
            "game": self.game_input.value.strip(),
            "price_display": self.price_input.value.strip(),
            "image_url": image_url,
            "platforms": platforms,
            "version": version,
            "author": author,
            "product_model": product_model,
            # Specs vides par défaut (à remplir avec /weightspecs)
            "specs_general": "",
            "specs_ae": "",
            "specs_win": "",
        }

        weights = load_weights()
        # Conserver les specs existantes si on édite
        if self.weight_id and self.weight_id in weights:
            for key in ["specs_general", "specs_ae", "specs_win"]:
                weight_data[key] = weights[self.weight_id].get(key, "")

        if not self.weight_id:
            # Générer un ID unique
            weight_id = re.sub(r'[^a-z0-9_]', '_', weight_data["name"].lower())
            weight_id = f"{weight_id}_{int(time.time())}"
        else:
            weight_id = self.weight_id

        weights[weight_id] = weight_data
        save_weights(weights)

        embed = build_weight_embed(weight_data)
        view = WeightView(weight_id)

        await interaction.response.send_message(
            f"✅ Weight **{weight_data['name']}** saved! Preview:",
            embed=embed,
            view=view,
            ephemeral=True,
        )


class WeightSpecsModal(discord.ui.Modal, title="Edit Specs"):

    def __init__(self, weight_id: str, existing: dict = None):
        super().__init__()
        self.weight_id = weight_id

        self.specs_general = discord.ui.TextInput(
            label="General specs",
            style=discord.TextStyle.paragraph,
            max_length=1000,
            required=False,
            placeholder="• Support Team Ignore\n• FOV: 120 (recommended)\n...",
            default=existing.get("specs_general", "") if existing else "",
        )
        self.specs_ae = discord.ui.TextInput(
            label="AE specs",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=False,
            placeholder="• Model config: [2,0]\n• Class 2: ~72%\n...",
            default=existing.get("specs_ae", "") if existing else "",
        )
        self.specs_win = discord.ui.TextInput(
            label="WIN specs",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=False,
            placeholder="• Target Classes: 2,0\n• Confidence: 50%\n...",
            default=existing.get("specs_win", "") if existing else "",
        )
        self.add_item(self.specs_general)
        self.add_item(self.specs_ae)
        self.add_item(self.specs_win)

    async def on_submit(self, interaction: discord.Interaction):
        weights = load_weights()
        if self.weight_id not in weights:
            await interaction.response.send_message("❌ Weight not found.", ephemeral=True)
            return

        weights[self.weight_id]["specs_general"] = self.specs_general.value.strip()
        weights[self.weight_id]["specs_ae"] = self.specs_ae.value.strip()
        weights[self.weight_id]["specs_win"] = self.specs_win.value.strip()
        save_weights(weights)

        embed = build_specs_embed(weights[self.weight_id])
        await interaction.response.send_message(
            f"✅ Specs updated for **{weights[self.weight_id]['name']}**! Preview:",
            embed=embed,
            ephemeral=True,
        )


# ─────────────────────────────────────────────
#  COMMANDES /weight
# ─────────────────────────────────────────────

async def weight_autocomplete(interaction: discord.Interaction, current: str):
    weights = load_weights()
    matches = [
        (wid, w["name"]) for wid, w in weights.items()
        if current.lower() in w["name"].lower()
    ]
    return [app_commands.Choice(name=name, value=wid) for wid, name in matches[:25]]


@tree.command(name="weight", description="Post a weight embed in this channel")
@app_commands.describe(weight_id="The weight to post")
@app_commands.autocomplete(weight_id=weight_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def weight_cmd(interaction: discord.Interaction, weight_id: str):
    weights = load_weights()
    weight_data = weights.get(weight_id)
    if not weight_data:
        await interaction.response.send_message("❌ Weight not found.", ephemeral=True)
        return

    embed = build_weight_embed(weight_data)
    view = WeightView(weight_id)

    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Weight posted!", ephemeral=True)


@tree.command(name="weightadd", description="Add or edit a weight (modal)")
@app_commands.describe(weight_id="Leave empty to create, or pick an existing weight to edit")
@app_commands.autocomplete(weight_id=weight_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def weightadd_cmd(interaction: discord.Interaction, weight_id: str = None):
    existing = None
    if weight_id:
        weights = load_weights()
        existing = weights.get(weight_id)
        if not existing:
            await interaction.response.send_message("❌ Weight not found.", ephemeral=True)
            return

    modal = WeightAddModal(weight_id=weight_id, existing=existing)
    await interaction.response.send_modal(modal)


@tree.command(name="weightspecs", description="Edit a weight's specs")
@app_commands.describe(weight_id="The weight whose specs you want to edit")
@app_commands.autocomplete(weight_id=weight_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def weightspecs_cmd(interaction: discord.Interaction, weight_id: str):
    weights = load_weights()
    weight_data = weights.get(weight_id)
    if not weight_data:
        await interaction.response.send_message("❌ Weight not found.", ephemeral=True)
        return
    modal = WeightSpecsModal(weight_id=weight_id, existing=weight_data)
    await interaction.response.send_modal(modal)


@tree.command(name="weightdelete", description="Delete a weight")
@app_commands.describe(weight_id="The weight to delete")
@app_commands.autocomplete(weight_id=weight_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def weightdelete_cmd(interaction: discord.Interaction, weight_id: str):
    weights = load_weights()
    if weight_id not in weights:
        await interaction.response.send_message("❌ Weight not found.", ephemeral=True)
        return
    name = weights[weight_id]["name"]
    del weights[weight_id]
    save_weights(weights)
    await interaction.response.send_message(f"🗑️ Weight **{name}** deleted.", ephemeral=True)


@tree.command(name="weightlist", description="List all registered weights")
@app_commands.checks.has_permissions(administrator=True)
async def weightlist_cmd(interaction: discord.Interaction):
    weights = load_weights()
    if not weights:
        await interaction.response.send_message("No weights registered.", ephemeral=True)
        return

    embed = discord.Embed(title="📦 Registered Weights", color=EMBED_COLOR)
    for wid, w in weights.items():
        platforms = " • ".join(p.upper() for p in w.get("platforms", []))
        embed.add_field(
            name=w["name"],
            value=f"ID: `{wid}`\nPlatforms: {platforms or 'N/A'}\nPrice: {w.get('price_display', 'N/A')}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
#  ORDER FLOW (Stripe)
# ─────────────────────────────────────────────

class ContactOnlyModal(discord.ui.Modal):
    def __init__(self, model_name: str, platform: str):
        label = "Your Discord ID"
        super().__init__(title="Order Details")
        self.model_name = model_name
        self.platform = platform
        self.contact_input = discord.ui.TextInput(label=label, max_length=200, required=True)
        self.add_item(self.contact_input)

    async def on_submit(self, interaction: discord.Interaction):
        products = load_products()
        if self.model_name not in products or self.platform not in products[self.model_name]:
            await interaction.response.send_message(
                "❌ This model/platform combination is no longer available. Please start over.",
                ephemeral=True,
            )
            return

        price_cents = products[self.model_name][self.platform]
        display_name = get_display_name(self.model_name)
        order_id = f"{interaction.user.id}_{int(time.time() * 1000)}"

        try:
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "eur",
                        "product_data": {"name": f"Weight: {display_name} ({self.platform.capitalize()})"},
                        "unit_amount": price_cents,
                    },
                    "quantity": 1,
                }],
                mode="payment",
                success_url="https://discord.com/channels/@me",
                cancel_url="https://discord.com/channels/@me",
                metadata={"order_id": order_id, "discord_user_id": str(interaction.user.id)},
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Stripe error: {str(e)}", ephemeral=True)
            return

        create_order(
            order_id=order_id,
            buyer_id=interaction.user.id,
            buyer_contact=str(self.contact_input.value),
            model=self.model_name,
            platform=self.platform,
            stripe_session_id=checkout_session.id,
            guild_id=interaction.guild_id,
        )

        embed = discord.Embed(
            title="💳 Complete Your Payment",
            description=(
                f"**Model:** {display_name}\n**Platform:** {self.platform.capitalize()}\n"
                f"**Price:** €{price_cents / 100:.2f}\n\n"
                f"Click below to pay securely via Stripe. "
                f"Once paid, your weight will be delivered automatically!"
            ),
            color=0xF1C40F,
        )
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Pay Now", url=checkout_session.url, style=discord.ButtonStyle.link))
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class PlatformSelect(discord.ui.Select):
    def __init__(self, model_name: str, available_platforms: dict):
        self.model_name = model_name
        options = [
            discord.SelectOption(
                label=platform.capitalize(),
                description=f"€{price / 100:.2f}",
                value=platform,
            )
            for platform, price in available_platforms.items()
        ]
        super().__init__(placeholder="Choose a platform...", options=options)

    async def callback(self, interaction: discord.Interaction):
        try:
            await interaction.response.send_modal(ContactOnlyModal(self.model_name, self.values[0]))
        except Exception as e:
            print(f"[PlatformSelect Error] {type(e).__name__}: {e}")
            try:
                await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
            except Exception:
                pass


class ModelSelect(discord.ui.Select):
    def __init__(self):
        products = load_products()
        options = []
        for name, platforms in products.items():
            if not platforms:
                continue
            min_price = min(platforms.values())
            display = get_display_name(name)
            platform_count = len(platforms)
            if platform_count > 1:
                desc = f"From €{min_price / 100:.2f} • {platform_count} platforms"
            else:
                only_platform = next(iter(platforms))
                desc = f"€{min_price / 100:.2f} • {only_platform.capitalize()}"
            options.append(discord.SelectOption(label=display, description=desc, value=name))
        options = options[:25]
        if not options:
            options = [discord.SelectOption(label="No products available", value="__none__")]
        super().__init__(placeholder="Choose a model...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message("❌ No products are currently available.", ephemeral=True)
            return
        model_name = self.values[0]
        products = load_products()
        available_platforms = products.get(model_name, {})
        if not available_platforms:
            await interaction.response.send_message("❌ This model is no longer available.", ephemeral=True)
            return
        if len(available_platforms) == 1:
            only_platform = next(iter(available_platforms))
            await interaction.response.send_modal(ContactOnlyModal(model_name, only_platform))
        else:
            platform_view = discord.ui.View(timeout=120)
            platform_view.add_item(PlatformSelect(model_name, available_platforms))
            await interaction.response.send_message(
                f"**{get_display_name(model_name)}** is available on multiple platforms. Which one?",
                view=platform_view,
                ephemeral=True,
            )


class OrderStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🛒 Order Now", style=discord.ButtonStyle.success, custom_id="winsight_order_start")
    async def order_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        select_view = discord.ui.View(timeout=120)
        select_view.add_item(ModelSelect())
        await interaction.response.send_message(
            "Which model would you like to order?", view=select_view, ephemeral=True
        )


# ─────────────────────────────────────────────
#  EVENTS
# ─────────────────────────────────────────────

@bot.event
async def on_ready():
    restored = await restore_config_from_discord()
    if not restored:
        await backup_config_to_discord("initial startup backup")

    await tree.sync()
    bot.add_view(OrderStartView())
    bot.add_view(TicketCloseView(0))
    bot.add_view(CrossPlatformPanelView())

    # Ré-enregistrer les WeightViews persistantes
    weights = load_weights()
    for weight_id in weights:
        bot.add_view(WeightView(weight_id))

    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"📡 Connected to {len(bot.guilds)} server(s): {', '.join(g.name for g in bot.guilds)}")

    # Panneau de commande : un par serveur qui l'a configuré
    for guild in bot.guilds:
        channel = get_guild_channel(guild.id, "order_panel_channel_id")
        if not channel or channel.guild.id != guild.id:
            continue
        embed = discord.Embed(title=ORDER_EMBED_TITLE, description=ORDER_EMBED_DESCRIPTION, color=EMBED_COLOR)
        try:
            await channel.send(embed=embed, view=OrderStartView())
            print(f"📌 Order panel posted in {guild.name}/#{channel.name}")
        except Exception as e:
            print(f"[Order Panel] Could not post in {guild.name}: {e}")


# ─────────────────────────────────────────────
#  COMMANDES ADMIN EXISTANTES
# ─────────────────────────────────────────────

@tree.command(name="setup", description="Configure the bot's channels for THIS server")
@app_commands.describe(
    staff_channel="Channel where the bot posts delivery reports and ticket alerts",
    order_panel_channel="Channel where the order panel is posted on startup",
    vouch_channel="Channel where customer reviews are posted",
    ticket_category="Category in which ticket channels are created",
)
@app_commands.checks.has_permissions(administrator=True)
async def setup_cmd(
    interaction: discord.Interaction,
    staff_channel: discord.TextChannel = None,
    order_panel_channel: discord.TextChannel = None,
    vouch_channel: discord.TextChannel = None,
    ticket_category: discord.CategoryChannel = None,
):
    if not interaction.guild:
        await interaction.response.send_message("❌ This command must be used inside a server.", ephemeral=True)
        return

    updates = {}
    if staff_channel:
        updates["staff_channel_id"] = staff_channel.id
    if order_panel_channel:
        updates["order_panel_channel_id"] = order_panel_channel.id
    if vouch_channel:
        updates["vouch_channel_id"] = vouch_channel.id
    if ticket_category:
        updates["ticket_category_id"] = ticket_category.id

    if updates:
        set_guild_config(interaction.guild.id, **updates)

    config = get_guild_config(interaction.guild.id)

    def show(key, is_category=False):
        value = config.get(key)
        if not value:
            return "*not configured*"
        channel = interaction.guild.get_channel(int(value))
        if channel:
            return channel.mention if not is_category else f"**{channel.name}**"
        return f"`{value}` *(not found on this server)*"

    embed = discord.Embed(
        title=f"⚙️ Configuration — {interaction.guild.name}",
        description="Run `/setup` again with the options you want to change." if updates
        else "Nothing changed. Pass channels as options to set them.",
        color=EMBED_COLOR,
    )
    embed.add_field(name="Staff", value=show("staff_channel_id"), inline=False)
    embed.add_field(name="Order panel", value=show("order_panel_channel_id"), inline=False)
    embed.add_field(name="Reviews (vouch)", value=show("vouch_channel_id"), inline=False)
    embed.add_field(name="Ticket category", value=show("ticket_category_id", True), inline=False)
    embed.set_footer(text="Weights, products and pipelines are shared across all servers.")

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="order", description="Post the Stripe order panel")
@app_commands.checks.has_permissions(administrator=True)
async def order_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title=ORDER_EMBED_TITLE, description=ORDER_EMBED_DESCRIPTION, color=EMBED_COLOR)
    await interaction.channel.send(embed=embed, view=OrderStartView())
    await interaction.response.send_message("✅ Order panel posted!", ephemeral=True)


platform_choices = [
    app_commands.Choice(name=PLATFORMS[key]["label"], value=key)
    for key in PLATFORMS
]


@tree.command(name="addproduct", description="Add or update a model/platform for sale")
@app_commands.choices(platform=platform_choices)
@app_commands.checks.has_permissions(administrator=True)
async def addproduct_cmd(interaction: discord.Interaction, model: str, price_eur: float, platform: app_commands.Choice[str]):
    products = load_products()
    if model not in products:
        products[model] = {}
    products[model][platform.value] = int(price_eur * 100)
    save_json(PRODUCTS_FILE, products)
    await interaction.response.send_message(
        f"✅ Product **{get_display_name(model)}** (`{model}`) set to €{price_eur:.2f} on **{platform.name}**.",
        ephemeral=True,
    )
    await backup_config_to_discord("addproduct")


@tree.command(name="removeproduct", description="Remove a model (or one specific platform) from sale")
@app_commands.describe(platform="Leave empty to remove the model from ALL platforms")
@app_commands.choices(platform=platform_choices)
@app_commands.checks.has_permissions(administrator=True)
async def removeproduct_cmd(interaction: discord.Interaction, model: str, platform: app_commands.Choice[str] = None):
    products = load_products()
    if model not in products:
        await interaction.response.send_message(f"⚠️ Model **{model}** not found.", ephemeral=True)
        return
    if platform:
        if platform.value in products[model]:
            del products[model][platform.value]
            if not products[model]:
                del products[model]
            save_json(PRODUCTS_FILE, products)
            await interaction.response.send_message(
                f"🚫 Removed **{get_display_name(model)}** from **{platform.name}**.", ephemeral=True
            )
            await backup_config_to_discord("removeproduct")
        else:
            await interaction.response.send_message(
                f"⚠️ **{model}** is not available on **{platform.name}**.", ephemeral=True
            )
    else:
        del products[model]
        save_json(PRODUCTS_FILE, products)
        await interaction.response.send_message(f"🚫 Removed **{get_display_name(model)}** from all platforms.", ephemeral=True)
        await backup_config_to_discord("removeproduct")


def build_product_list_embed() -> discord.Embed:
    products = load_products()
    embed = discord.Embed(
        title="🛒 Available Weights",
        description="Here's everything currently available for purchase:",
        color=EMBED_COLOR,
    )
    if not products:
        embed.description = "No products are currently available."
        return embed
    for name, platforms in products.items():
        if not platforms:
            continue
        display = get_display_name(name)
        lines = "\n".join(
            f"● **{platform.capitalize()}** — €{price / 100:.2f}"
            for platform, price in platforms.items()
        )
        embed.add_field(name=display, value=lines, inline=False)
    embed.set_footer(text="Use the Order Now button to purchase!")
    return embed


@tree.command(name="productlist", description="Show the list of available models")
@app_commands.describe(public="Post publicly in the channel (default: only visible to you)")
async def productlist_cmd(interaction: discord.Interaction, public: bool = False):
    embed = build_product_list_embed()
    await interaction.response.send_message(embed=embed, ephemeral=not public)


# ─────────────────────────────────────────────
#  CLIENT API WINSIGHT
# ─────────────────────────────────────────────
#
#  Le portail Winsight expose une API REST — la même que son propre front-end
#  React consomme. On tape dessus directement plutôt que de piloter un
#  navigateur : pas de Chromium à installer, pas de sélecteurs DOM qui cassent
#  au prochain déploiement du site, et une vraie erreur exploitable au lieu
#  d'un « j'ai cliqué quelque part ».

WINSIGHT_API_PREFIX = "/api/n4f7a2c6d1e8x5b3"
WINSIGHT_WEIGHTS_TTL = 30  # secondes de cache sur la liste des weights


def _winsight_api_base() -> str:
    """Origine du portail (https://host), déduite de WINSIGHT_URL."""
    explicit = os.environ.get("WINSIGHT_API_BASE", "").strip()
    if explicit:
        return explicit.rstrip("/")
    parsed = urlparse(WINSIGHT_URL)
    return f"{parsed.scheme}://{parsed.netloc}"


def normalize_weight_name(name: str) -> str:
    """Normalise pour comparaison : casse, extension .onnx, `_`, `-` et espaces."""
    name = (name or "").strip().lower()
    if name.endswith(".onnx"):
        name = name[:-5]
    return re.sub(r"[_\-\s]", "", name)


class WinsightError(Exception):
    """Erreur métier renvoyée par le portail (message déjà lisible)."""


class WinsightClient:
    """
    Client HTTP du portail Winsight : session persistante (cookie), login à la
    demande, et re-login automatique si la session expire.
    """

    def __init__(self):
        self._session = None
        self._lock = asyncio.Lock()
        self._authenticated = False
        self._weights = None
        self._weights_at = 0.0

    # ── plomberie ────────────────────────────

    def _url(self, path: str) -> str:
        return f"{_winsight_api_base()}{WINSIGHT_API_PREFIX}{path}"

    async def _get_session(self):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
                cookie_jar=aiohttp.CookieJar(),
            )
            self._authenticated = False
        return self._session

    async def _login(self):
        if not WINSIGHT_USERNAME or not WINSIGHT_PASSWORD:
            raise WinsightError("WINSIGHT_USERNAME / WINSIGHT_PASSWORD are not configured.")
        session = await self._get_session()
        async with session.post(
            self._url("/login"),
            json={"username": WINSIGHT_USERNAME, "password": WINSIGHT_PASSWORD},
        ) as resp:
            body = await _json_or_none(resp)
            if resp.status == 200:
                print("[Winsight] Session ouverte.")
                self._weights = None  # nouvelle session → cache invalidé
                return
            message = ""
            if isinstance(body, dict):
                message = body.get("message") or body.get("error") or ""
            raise WinsightError(message or f"Login rejected (HTTP {resp.status}).")

    async def _ensure_auth(self):
        if self._authenticated:
            return
        async with self._lock:
            if self._authenticated:
                return
            await self._login()
            self._authenticated = True

    async def _request(self, method: str, path: str, json_body=None, _retry: bool = True):
        await self._ensure_auth()
        session = await self._get_session()
        async with session.request(method, self._url(path), json=json_body) as resp:
            if resp.status in (401, 403) and _retry:
                # Session expirée côté serveur : on se reconnecte une fois.
                self._authenticated = False
                return await self._request(method, path, json_body, _retry=False)
            body = await _json_or_none(resp)
            if resp.status >= 400:
                message = ""
                if isinstance(body, dict):
                    message = body.get("message") or body.get("error") or ""
                raise WinsightError(message or f"HTTP {resp.status}")
            return body

    # ── weights ──────────────────────────────

    async def list_weights(self, force: bool = False) -> list:
        now = time.time()
        if not force and self._weights is not None and now - self._weights_at < WINSIGHT_WEIGHTS_TTL:
            return self._weights
        data = await self._request("GET", "/weights")
        self._weights = data if isinstance(data, list) else []
        self._weights_at = now
        return self._weights

    async def find_weights(self, keyword: str) -> list:
        """Toutes les weights dont le nom contient `keyword`."""
        target = normalize_weight_name(keyword)
        if not target:
            return []
        return [
            w for w in await self.list_weights()
            if target in normalize_weight_name(w.get("fileName", ""))
        ]

    async def find_weight(self, model_name: str) -> dict:
        """
        Une seule weight : correspondance exacte en priorité, sinon l'unique
        correspondance partielle. On refuse plutôt que de deviner — donner le
        mauvais modèle à un client payant est pire qu'une erreur claire.
        """
        target = normalize_weight_name(model_name)
        weights = await self.list_weights()

        exact = [w for w in weights if normalize_weight_name(w.get("fileName", "")) == target]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise WinsightError(f"'{model_name}' matches {len(exact)} weights with the same name.")

        partial = [
            w for w in weights
            if target and target in normalize_weight_name(w.get("fileName", ""))
        ]
        if len(partial) == 1:
            return partial[0]
        if not partial:
            raise WinsightError(f"'{model_name}' not found on Winsight.")
        names = ", ".join(w.get("fileName", "?") for w in partial[:5])
        raise WinsightError(f"'{model_name}' is ambiguous ({len(partial)} matches: {names}).")

    # ── partages ─────────────────────────────

    async def list_shares(self, weight_id) -> list:
        data = await self._request("GET", f"/weights/{weight_id}/shares")
        return data if isinstance(data, list) else []

    async def has_share(self, weight_id, username: str) -> bool:
        wanted = str(username).strip().lower()
        return any(
            str(s.get("sharedWithUsername", "")).strip().lower() == wanted
            for s in await self.list_shares(weight_id)
        )

    async def share(self, weight_id, username: str):
        await self._request(
            "POST", f"/weights/{weight_id}/shares", {"shareUsername": str(username).strip()}
        )

    async def unshare(self, weight_id, username: str):
        await self._request(
            "DELETE", f"/weights/{weight_id}/shares/{quote(str(username).strip(), safe='')}"
        )


async def _json_or_none(resp):
    try:
        return await resp.json(content_type=None)
    except Exception:
        return None


winsight = WinsightClient()


# ─────────────────────────────────────────────
#  OPÉRATIONS WINSIGHT (grant / check / revoke)
# ─────────────────────────────────────────────

async def _winsight_share_one(weight: dict, username: str) -> tuple[bool, str]:
    """Partage une weight déjà résolue. Idempotent."""
    name = weight.get("fileName", f"#{weight.get('id')}")
    try:
        if await winsight.has_share(weight["id"], username):
            return True, f"✓ {name} (already shared)"
        await winsight.share(weight["id"], username)
        return True, f"✓ {name}"
    except Exception as e:
        return False, f"✗ {name} — {e}"


async def winsight_grant(discord_id: str, model_name: str) -> tuple[bool, str]:
    """Partage un seul modèle avec un client."""
    print(f"[Winsight] Grant: discord_id={discord_id}, model={model_name}")
    try:
        weight = await winsight.find_weight(model_name)
    except Exception as e:
        return False, f"✗ {e}"

    ok, msg = await _winsight_share_one(weight, discord_id)
    if ok:
        return True, f"Access granted to {discord_id} for {weight.get('fileName', model_name)} on Winsight."
    return False, msg


async def winsight_grant_all_dynamic(discord_id: str, keyword: str) -> tuple[int, int, list[str]]:
    """
    Partage toutes les weights dont le nom contient `keyword` (ex: « Valorant »
    → toutes les versions). Retourne (nb_ok, nb_fail, détails).
    """
    print(f"[Winsight] Grant ALL: discord_id={discord_id}, keyword='{keyword}'")
    try:
        matches = await winsight.find_weights(keyword)
    except Exception as e:
        return 0, 1, [f"✗ Winsight API error: {e}"]

    if not matches:
        return 0, 0, [f"⚠️ No weight matching '{keyword}' found on Winsight."]

    successes = 0
    failures = 0
    details = []
    for weight in matches:
        ok, msg = await _winsight_share_one(weight, discord_id)
        successes += 1 if ok else 0
        failures += 0 if ok else 1
        details.append(msg)
    return successes, failures, details


async def winsight_grant_all(discord_id: str, model_names: list[str]) -> tuple[int, int, list[str]]:
    """Partage une liste fixe de modèles (noms connus à l'avance)."""
    print(f"[Winsight] Grant ALL (fixed list): discord_id={discord_id}, models={model_names}")
    successes = 0
    failures = 0
    details = []
    for model_name in model_names:
        try:
            weight = await winsight.find_weight(model_name)
        except Exception as e:
            failures += 1
            details.append(f"✗ {model_name} — {e}")
            continue
        ok, msg = await _winsight_share_one(weight, discord_id)
        successes += 1 if ok else 0
        failures += 0 if ok else 1
        details.append(msg)
    return successes, failures, details


async def winsight_grant_pipeline(discord_id: str, pipeline_site_name: str) -> tuple[bool, str]:
    success, message = await winsight_grant(discord_id, pipeline_site_name)
    if success:
        return True, f"Pipeline access granted to {discord_id} for {pipeline_site_name} on Winsight."
    return False, message


async def winsight_check(discord_id: str, model_name: str) -> tuple[bool, str]:
    """Vérifie si un client figure dans les partages d'une weight."""
    try:
        weight = await winsight.find_weight(model_name)
        name = weight.get("fileName", model_name)
        if await winsight.has_share(weight["id"], discord_id):
            return True, f"✅ {discord_id} has access to **{name}**."
        return False, f"❌ {discord_id} does NOT have access to **{name}**."
    except Exception as e:
        return False, f"⚠️ {e}"


async def winsight_revoke(discord_id: str, model_name: str) -> tuple[bool, str]:
    """Retire l'accès d'un client à une weight, puis vérifie que c'est effectif."""
    print(f"[Winsight] Revoke: discord_id={discord_id}, model={model_name}")
    try:
        weight = await winsight.find_weight(model_name)
    except Exception as e:
        return False, f"✗ {e}"

    name = weight.get("fileName", model_name)
    try:
        if not await winsight.has_share(weight["id"], discord_id):
            return False, f"⚠️ {discord_id} does not have access to **{name}**."
        await winsight.unshare(weight["id"], discord_id)
        # On relit la liste : c'est la seule preuve que le retrait a bien eu lieu.
        if await winsight.has_share(weight["id"], discord_id):
            return False, f"✗ {name} (share still active after removal)"
        return True, f"✓ {name} — access revoked for {discord_id}"
    except Exception as e:
        return False, f"✗ {name} — {e}"


# ─────────────────────────────────────────────
#  HELIOS / INPUTSENSE
# ─────────────────────────────────────────────
#
#  Chaque modèle a son propre « item » Helios (un UUID). Le mapping
#  modèle → resource_id vit dans helios_resources.json, alimenté par
#  /setheliosresource. Sans mapping, on retombe sur HELIOS_RESOURCE_ID.

def _helios_headers(json_body: bool = True) -> dict:
    headers = {"Authorization": f"Bearer {HELIOS_API_KEY}"}
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _helios_resolve(model_name, resource_id):
    """Retourne (resource_id, erreur) — l'un des deux est None."""
    if not HELIOS_API_KEY:
        return None, "❌ HELIOS_API_KEY not configured."
    rid = resource_id or get_helios_resource(model_name)
    if not rid:
        target = model_name or "this model"
        return None, f"❌ No Helios resource ID configured for **{target}** (use `/setheliosresource`)."
    return rid, None


def _helios_error(body) -> str:
    error = body.get("error", {}) if isinstance(body, dict) else {}
    return f"❌ Helios: {error.get('code', 'unknown')} — {error.get('message', str(body))}"


async def helios_grant(discord_id: str, model_name: str = None, notes: str = None,
                       resource_id: str = None) -> tuple[bool, str]:
    """Grant access via Helios/InputSense API."""
    rid, err = _helios_resolve(model_name, resource_id)
    if err:
        return False, err

    url = f"{HELIOS_API_URL}?route=items/{rid}/authorized-users"
    payload = {
        "discord_id": str(discord_id),
        "notes": notes or "Granted via bot",
        "expires_at": None,
    }
    label = model_name or "Helios"
    print(f"[Helios] Grant: item={rid} model={model_name} discord_id={discord_id}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=_helios_headers(), json=payload,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body = await resp.json(content_type=None)
                print(f"[Helios] Response: {resp.status} {body}")
                if isinstance(body, dict) and body.get("success"):
                    return True, f"✅ Access granted to `{discord_id}` for **{label}**."
                return False, _helios_error(body)
    except Exception as e:
        print(f"[Helios] EXCEPTION: {e}")
        return False, f"Error: {str(e)}"


async def helios_revoke(discord_id: str, model_name: str = None,
                        resource_id: str = None) -> tuple[bool, str]:
    """Revoke access via Helios/InputSense API."""
    rid, err = _helios_resolve(model_name, resource_id)
    if err:
        return False, err

    url = f"{HELIOS_API_URL}?route=items/{rid}/authorized-users/{discord_id}/revoke"
    label = model_name or "Helios"
    print(f"[Helios] Revoke: item={rid} model={model_name} discord_id={discord_id}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=_helios_headers(), json={},
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body = await resp.json(content_type=None)
                print(f"[Helios] Response: {resp.status} {body}")
                if isinstance(body, dict) and body.get("success"):
                    return True, f"✅ Access revoked for `{discord_id}` on **{label}**."
                return False, _helios_error(body)
    except Exception as e:
        print(f"[Helios] EXCEPTION: {e}")
        return False, f"Error: {str(e)}"


async def helios_check(discord_id: str, model_name: str = None,
                      resource_id: str = None) -> tuple[bool, str]:
    """Check if a user has access via Helios/InputSense API."""
    rid, err = _helios_resolve(model_name, resource_id)
    if err:
        return False, err

    label = model_name or "Helios"
    wanted = str(discord_id).strip()
    url = f"{HELIOS_API_URL}?route=items/{rid}/authorized-users&limit=200&offset=0"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_helios_headers(False),
                                   timeout=aiohttp.ClientTimeout(total=15)) as resp:
                body = await resp.json(content_type=None)
                if not (isinstance(body, dict) and body.get("success")):
                    return False, f"❌ Helios API error: {body}"
                for user in body.get("data", []):
                    if str(user.get("discord_id", "")).strip() == wanted:
                        return True, f"✅ `{discord_id}` has access to **{label}**."
                return False, f"❌ `{discord_id}` does NOT have access to **{label}**."
    except Exception as e:
        return False, f"Error: {str(e)}"

async def process_paid_order(order_id: str):
    order = get_order(order_id)
    if not order:
        return
    update_order(order_id, status="processing")
    platform = order.get("platform", "winsight")

    if platform == "helios":
        success, message = await helios_grant(order["buyer_contact"], order["model"])
    else:
        success, message = await winsight_grant(order["buyer_contact"], order["model"])

    update_order(order_id, status="delivered" if success else "failed")

    # Rapport dans le salon staff du serveur d'où vient la commande
    channel = get_guild_channel(order.get("guild_id"), "staff_channel_id")
    if channel:
        color = 0x57F287 if success else 0xED4245
        title = "✅ Order Delivered Automatically" if success else "❌ Auto-Delivery Failed"
        embed = discord.Embed(title=title, color=color)
        embed.add_field(name="Buyer", value=f"<@{order['buyer_id']}>", inline=True)
        embed.add_field(name="Contact", value=order["buyer_contact"], inline=True)
        embed.add_field(name="Model", value=get_display_name(order["model"]), inline=False)
        embed.add_field(name="Details", value=message, inline=False)
        embed.set_footer(text=f"Order ID: {order_id}")
        await channel.send(embed=embed)

    buyer = bot.get_user(order["buyer_id"])
    if buyer:
        try:
            if success:
                await buyer.send(
                    f"✅ Your payment was received and **{get_display_name(order['model'])}** has been added to your account!"
                )
            else:
                await buyer.send(
                    f"⚠️ Your payment for **{get_display_name(order['model'])}** was received, but automatic delivery failed. "
                    f"Our team has been notified and will resolve this manually."
                )
        except discord.Forbidden:
            pass


# ─────────────────────────────────────────────
#  COMMANDES MANUELLES
# ─────────────────────────────────────────────

async def model_autocomplete(interaction: discord.Interaction, current: str):
    """
    Autocomplete groupé par jeu : affiche "Valorant" (pas "Valorant V2" / "Valorant V3" séparément).
    La value est le nom du groupe (ex: "Valorant").
    """
    groups = get_grouped_products()
    matches = [
        group for group in groups.keys()
        if current.lower() in group.lower()
    ]
    return [app_commands.Choice(name=group, value=group) for group in matches[:25]]


async def pipeline_autocomplete(interaction: discord.Interaction, current: str):
    pipelines = load_pipelines()
    matches = [name for name in pipelines.keys() if current.lower() in name.lower()]
    return [app_commands.Choice(name=name, value=name) for name in matches[:25]]


def get_platforms_for_group(group_name: str) -> list:
    """Retourne toutes les plateformes disponibles pour un groupe (union de toutes les versions)."""
    products = load_products()
    models = get_models_for_group(group_name)
    platforms = set()
    for model in models:
        platforms.update(products.get(model, {}).keys())
    return list(platforms)


def get_platforms_for_model(model_name: str) -> list:
    return list(load_products().get(model_name, {}).keys())


@tree.command(name="setpipeline", description="Register a pipeline's exact name on Winsight")
@app_commands.checks.has_permissions(administrator=True)
async def setpipeline_cmd(interaction: discord.Interaction, pipeline: str, site_name: str):
    pipelines = load_pipelines()
    pipelines[pipeline] = site_name
    save_pipelines(pipelines)
    await interaction.response.send_message(
        f"✅ Pipeline **{pipeline}** → `{site_name}`.", ephemeral=False
    )
    await backup_config_to_discord("setpipeline")


@tree.command(name="removepipeline", description="Delete a registered pipeline")
@app_commands.autocomplete(pipeline=pipeline_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def removepipeline_cmd(interaction: discord.Interaction, pipeline: str):
    pipelines = load_pipelines()
    if pipeline not in pipelines:
        await interaction.response.send_message(f"⚠️ Pipeline **{pipeline}** not found.", ephemeral=False)
        return
    del pipelines[pipeline]
    save_pipelines(pipelines)
    await interaction.response.send_message(f"🚫 Pipeline **{pipeline}** deleted.", ephemeral=False)
    await backup_config_to_discord("removepipeline")


@tree.command(name="pipelinelist", description="Show registered pipelines")
@app_commands.checks.has_permissions(administrator=True)
async def pipelinelist_cmd(interaction: discord.Interaction):
    pipelines = load_pipelines()
    if not pipelines:
        await interaction.response.send_message("No pipelines registered.", ephemeral=False)
        return
    lines = [f"● **{name}** → `{site_name}`" for name, site_name in pipelines.items()]
    embed = discord.Embed(title="Winsight Pipelines", description="\n".join(lines), color=EMBED_COLOR)
    await interaction.response.send_message(embed=embed, ephemeral=False)


@tree.command(name="pipelineadd", description="Give a Winsight pipeline to a user")
@app_commands.autocomplete(pipeline=pipeline_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def pipelineadd_cmd(interaction: discord.Interaction, pipeline: str, discord_id: str):
    pipelines = load_pipelines()
    if pipeline not in pipelines:
        await interaction.response.send_message(
            f"❌ Pipeline **{pipeline}** not found.", ephemeral=False
        )
        return
    site_name = pipelines[pipeline]
    await interaction.response.send_message(
        f"⏳ Adding pipeline **{pipeline}** to `{discord_id}`...", ephemeral=False
    )

    async def run():
        success, message = await winsight_grant_pipeline(discord_id, site_name)
        embed = discord.Embed(
            title=f"✅ Pipeline Added — {pipeline}" if success else "❌ Pipeline Add Failed",
            description=message,
            color=0x57F287 if success else 0xED4245,
        )
        embed.add_field(name="User", value=f"<@{discord_id}> ({discord_id})", inline=False)
        embed.add_field(name="Winsight Name", value=site_name, inline=False)
        embed.set_footer(text=f"Pipeline Add • by {interaction.user}")
        await interaction.followup.send(embed=embed, ephemeral=False)

    asyncio.create_task(run())


@tree.command(name="grantaccess", description="Manually grant access to a game (all versions at once)")
@app_commands.describe(
    model="The game to grant (e.g. Valorant → grants every Valorant version)",
    platform="The platform",
    contact="Customer's Discord ID",
)
@app_commands.autocomplete(model=model_autocomplete)
@app_commands.choices(platform=platform_choices)
@app_commands.checks.has_permissions(administrator=True)
async def grantaccess_cmd(interaction: discord.Interaction, model: str, platform: app_commands.Choice[str], contact: str):
    group_name = model

    available_platforms = get_platforms_for_group(group_name)
    if platform.value not in available_platforms:
        await interaction.response.send_message(
            f"❌ **{group_name}** not configured on **{platform.name}**. "
            f"Available: {', '.join(platform_label(p) for p in available_platforms) or 'none'}",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"⏳ Granting **{group_name}** on **{platform.name}** to `{contact}`...",
        ephemeral=False,
    )

    async def run():
        if platform.value == "helios":
            # Helios : un item par modèle, résolu via helios_resources.json
            models = get_models_on_platform(group_name, "helios")
            details = []
            nb_ok = 0
            for m in models:
                ok, msg = await helios_grant(contact, m)
                details.append(f"{'✓' if ok else '✗'} {get_display_name(m)} — {msg}")
                if ok:
                    nb_ok += 1
            nb_fail = len(models) - nb_ok
        else:
            # Winsight : scraping dynamique
            nb_ok, nb_fail, details = await winsight_grant_all_dynamic(contact, group_name)

        all_ok = nb_fail == 0
        color = 0x57F287 if all_ok else (0xF1C40F if nb_ok > 0 else 0xED4245)
        prefix = platform_label(platform.value)

        if all_ok:
            title = f"✅ {prefix} Granted — {group_name}"
        elif nb_ok > 0:
            title = f"⚠️ {prefix} Partial Grant — {group_name}"
        else:
            title = f"❌ {prefix} Grant Failed — {group_name}"

        embed = discord.Embed(title=title, color=color)

        embed.add_field(name="User", value=f"<@{contact}> ({contact})", inline=False)

        embed.add_field(
            name="Result",
            value=f"{nb_ok} granted, {nb_fail} failed",
            inline=False,
        )
        embed.add_field(
            name="Details",
            value="\n".join(details) or "—",
            inline=False,
        )
        embed.set_footer(text=f"Manual Grant • by {interaction.user}")

        # Poster publiquement dans le salon où la commande a été faite
        await interaction.channel.send(embed=embed)
        # Confirmer à l'auteur que c'est envoyé (ephemeral, juste pour lui)
        await interaction.followup.send("✅ Result posted above.", ephemeral=True)

    asyncio.create_task(run())


@tree.command(name="checkaccess", description="Check whether a customer has access to a game (all versions)")
@app_commands.autocomplete(model=model_autocomplete)
@app_commands.choices(platform=platform_choices)
@app_commands.checks.has_permissions(administrator=True)
async def checkaccess_cmd(interaction: discord.Interaction, model: str, platform: app_commands.Choice[str], contact: str):
    group_name = model
    available_platforms = get_platforms_for_group(group_name)
    if platform.value not in available_platforms:
        await interaction.response.send_message(
            f"❌ **{group_name}** not configured on **{platform.name}**.", ephemeral=True
        )
        return

    models_on_platform = get_models_on_platform(group_name, platform.value)

    await interaction.response.send_message(
        f"⏳ Checking **{group_name}** ({len(models_on_platform)} version(s)) for `{contact}`...", ephemeral=True
    )

    async def run():
        lines = []
        for m in models_on_platform:
            if platform.value == "helios":
                _, msg = await helios_check(contact, m)
            else:
                _, msg = await winsight_check(contact, m)
            lines.append(f"**{get_display_name(m)}**: {msg}")
        embed = discord.Embed(
            title=f"🔍 Access Check — {group_name}",
            description="\n".join(lines) or "No models found.",
            color=0x5865F2,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    asyncio.create_task(run())


@tree.command(name="revoke", description="Revoke a customer's access (Winsight only, all versions)")
@app_commands.autocomplete(model=model_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def revoke_cmd(interaction: discord.Interaction, model: str, contact: str):
    group_name = model
    available_platforms = get_platforms_for_group(group_name)
    if "winsight" not in available_platforms:
        await interaction.response.send_message(
            f"❌ **{group_name}** not on Winsight.", ephemeral=True
        )
        return

    all_models = get_models_for_group(group_name)
    products = load_products()
    models_on_winsight = [m for m in all_models if "winsight" in products.get(m, {})]

    await interaction.response.send_message(
        f"⏳ Revoking **{group_name}** ({len(models_on_winsight)} version(s)) for `{contact}`...", ephemeral=True
    )

    async def run():
        lines = []
        nb_ok = 0
        for m in models_on_winsight:
            ok, msg = await winsight_revoke(contact, m)
            lines.append(f"{'✓' if ok else '✗'} {get_display_name(m)}: {msg}")
            if ok:
                nb_ok += 1
        nb_fail = len(models_on_winsight) - nb_ok
        all_ok = nb_fail == 0
        embed = discord.Embed(
            title=f"{'✅' if all_ok else '⚠️'} Revoke — {group_name}",
            description="\n".join(lines),
            color=0x57F287 if all_ok else 0xF1C40F,
        )
        embed.add_field(name="Result", value=f"{nb_ok} revoked, {nb_fail} failed", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    asyncio.create_task(run())


# ─────────────────────────────────────────────
#  CROSS-PLATFORM ACCESS
# ─────────────────────────────────────────────
#
#  Le client clique un bouton, choisit la plateforme où il possède déjà le
#  modèle, on vérifie via API ce qu'il possède réellement, puis il choisit la
#  plateforme cible et on lui grant tout ce qui existe là-bas.
#
#  L'identifiant utilisé est l'ID Discord de celui qui clique — c'est déjà la
#  clé de partage côté Winsight comme côté Helios.

CROSS_PLATFORM_TITLE = "🔀 Cross-Platform Access"
CROSS_PLATFORM_DESCRIPTION = (
    "Already own a weight on one platform and want it on another too? "
    "Press the button below or use `/cross-platform-access` right here in this channel.\n\n"
    "**How it works:**\n"
    "1️⃣ Pick the platform you currently own it on\n"
    "2️⃣ We'll automatically check what you own\n"
    "3️⃣ Pick the platform you'd like access on\n"
    "4️⃣ Everything you own gets added there right away\n\n"
    "ℹ️ Your existing access isn't touched or removed — this only adds access elsewhere.\n\n"
    "⚠️ Make sure your subscription is linked to **this** Discord account before starting."
)


async def get_owned_models(platform: str, discord_id: str) -> list[str]:
    """
    Modèles du catalogue que ce client possède sur `platform`.

    On ne teste que les modèles présents dans products.json pour cette
    plateforme : c'est le catalogue vendu, donc le seul périmètre pertinent.
    """
    products = load_products()
    candidates = [m for m in products if platform in products.get(m, {})]
    owned = []

    for model_name in candidates:
        if platform == "winsight":
            try:
                weight = await winsight.find_weight(model_name)
            except WinsightError:
                continue  # pas (encore) sur le portail, ou nom ambigu
            if await winsight.has_share(weight["id"], discord_id):
                owned.append(model_name)
        else:
            ok, _ = await helios_check(discord_id, model_name)
            if ok:
                owned.append(model_name)

    return owned


async def grant_model_on_platform(platform: str, discord_id: str, model_name: str) -> tuple[bool, str]:
    if platform == "winsight":
        return await winsight_grant(discord_id, model_name)
    return await helios_grant(discord_id, model_name)


def _platform_options(keys) -> list[discord.SelectOption]:
    return [
        discord.SelectOption(
            label=PLATFORMS[k]["label"],
            value=k,
            emoji=PLATFORMS[k]["emoji"],
        )
        for k in keys
    ]


class CrossPlatformSourceSelect(discord.ui.Select):
    """Étape 1 : où le client possède déjà le modèle."""

    def __init__(self):
        super().__init__(
            placeholder="Platform you already own it on…",
            options=_platform_options(PLATFORMS.keys()),
        )

    async def callback(self, interaction: discord.Interaction):
        source = self.values[0]
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            owned = await get_owned_models(source, discord_id)
        except Exception as e:
            print(f"[CrossPlatform] Ownership check failed on {source}: {e}")
            await interaction.followup.send(
                f"❌ Couldn't reach **{platform_label(source)}** right now. Please try again in a moment.",
                ephemeral=True,
            )
            return

        if not owned:
            await interaction.followup.send(
                f"❌ We couldn't find any weight you own on **{platform_label(source)}** "
                f"for this Discord account (`{discord_id}`).\n\n"
                "Make sure your subscription is linked to this account, then try again. "
                "If you're sure it should be there, open a ticket.",
                ephemeral=True,
            )
            return

        products = load_products()
        targets = [
            key for key in PLATFORMS
            if key != source and any(key in products.get(m, {}) for m in owned)
        ]

        found = "\n".join(f"● **{get_display_name(m)}**" for m in owned)

        if not targets:
            await interaction.followup.send(
                f"✅ You own **{len(owned)}** weight(s) on {platform_label(source)}:\n{found}\n\n"
                "❌ None of them are available on another platform, so there's nothing to transfer.",
                ephemeral=True,
            )
            return

        view = discord.ui.View(timeout=300)
        view.add_item(CrossPlatformTargetSelect(source, owned, targets))
        await interaction.followup.send(
            f"**Step 2 of 2** — Found **{len(owned)}** weight(s) on {platform_label(source)}:\n{found}\n\n"
            "Where would you like access?",
            view=view,
            ephemeral=True,
        )


class CrossPlatformTargetSelect(discord.ui.Select):
    """Étape 2 : où le client veut l'accès."""

    def __init__(self, source: str, owned: list[str], targets: list[str]):
        self.source = source
        self.owned = owned
        super().__init__(
            placeholder="Platform you want access on…",
            options=_platform_options(targets),
        )

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        discord_id = str(interaction.user.id)
        await interaction.response.defer(ephemeral=True, thinking=True)

        products = load_products()
        todo = [m for m in self.owned if target in products.get(m, {})]

        lines = []
        nb_ok = 0
        for model_name in todo:
            ok, msg = await grant_model_on_platform(target, discord_id, model_name)
            lines.append(f"{'✓' if ok else '✗'} **{get_display_name(model_name)}**")
            if ok:
                nb_ok += 1
            else:
                print(f"[CrossPlatform] Grant failed {model_name} on {target}: {msg}")

        nb_fail = len(todo) - nb_ok
        all_ok = nb_fail == 0 and nb_ok > 0

        embed = discord.Embed(
            title="✅ Access Added" if all_ok else ("⚠️ Partially Added" if nb_ok else "❌ Nothing Added"),
            description=(
                f"**{platform_label(self.source)}** → **{platform_label(target)}**\n\n"
                + "\n".join(lines)
            ),
            color=0x57F287 if all_ok else (0xF1C40F if nb_ok else 0xED4245),
        )
        embed.add_field(name="Result", value=f"{nb_ok} added, {nb_fail} failed", inline=False)
        if nb_ok:
            embed.set_footer(text="Your access on the original platform is unchanged.")
        if nb_fail:
            embed.add_field(
                name="Need help?",
                value="Open a ticket and staff will sort out the rest manually.",
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

        staff = get_guild_channel(interaction.guild_id, "staff_channel_id")
        if staff:
            report = discord.Embed(
                title="🔀 Cross-Platform Transfer",
                description=f"**{platform_label(self.source)}** → **{platform_label(target)}**\n"
                            + "\n".join(lines),
                color=embed.color,
            )
            report.add_field(name="Customer", value=f"{interaction.user.mention} (`{discord_id}`)", inline=False)
            report.add_field(name="Result", value=f"{nb_ok} added, {nb_fail} failed", inline=False)
            try:
                await staff.send(embed=report)
            except Exception as e:
                print(f"[CrossPlatform] Could not post staff report: {e}")


async def start_cross_platform(interaction: discord.Interaction):
    if not load_products():
        await interaction.response.send_message(
            "⚠️ No products are configured yet. Ask an admin to set them up.", ephemeral=True
        )
        return
    view = discord.ui.View(timeout=300)
    view.add_item(CrossPlatformSourceSelect())
    await interaction.response.send_message(
        "**Step 1 of 2** — Which platform do you currently own the weight on?",
        view=view,
        ephemeral=True,
    )


class CrossPlatformPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Cross-Platform Access",
        emoji="🔀",
        style=discord.ButtonStyle.primary,
        custom_id="cross_platform_start",
    )
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await start_cross_platform(interaction)


@tree.command(name="cross-platform-access", description="Get a weight you own on another platform too")
async def cross_platform_access_cmd(interaction: discord.Interaction):
    await start_cross_platform(interaction)


@tree.command(name="crossplatformpanel", description="Post the Cross-Platform Access panel in this channel")
@app_commands.checks.has_permissions(administrator=True)
async def crossplatformpanel_cmd(interaction: discord.Interaction):
    embed = discord.Embed(
        title=CROSS_PLATFORM_TITLE,
        description=CROSS_PLATFORM_DESCRIPTION,
        color=0x5865F2,
    )
    platforms = " • ".join(f"{cfg['emoji']} {cfg['label']}" for cfg in PLATFORMS.values())
    embed.add_field(name="Supported platforms", value=platforms, inline=False)
    await interaction.channel.send(embed=embed, view=CrossPlatformPanelView())
    await interaction.response.send_message("✅ Cross-platform panel posted!", ephemeral=True)


# ─────────────────────────────────────────────
#  HELIOS RESOURCE MAPPING (admin)
# ─────────────────────────────────────────────

async def product_autocomplete(interaction: discord.Interaction, current: str):
    """Autocomplete sur les noms de modèles exacts (pas les groupes de jeu)."""
    matches = [n for n in load_products().keys() if current.lower() in n.lower()]
    return [app_commands.Choice(name=n, value=n) for n in matches[:25]]


@tree.command(name="setheliosresource", description="Map a model to its Helios resource ID (UUID)")
@app_commands.describe(
    model="Exact model name (e.g. XyCubValorantV2)",
    resource_id="The Helios item UUID for this model",
)
@app_commands.autocomplete(model=product_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def setheliosresource_cmd(interaction: discord.Interaction, model: str, resource_id: str):
    resources = load_helios_resources()
    resources[model.strip()] = resource_id.strip()
    save_helios_resources(resources)
    await interaction.response.send_message(
        f"✅ **{get_display_name(model)}** (`{model}`) → Helios item `{resource_id.strip()}`.",
        ephemeral=True,
    )
    await backup_config_to_discord("setheliosresource")


@tree.command(name="removeheliosresource", description="Remove a model's Helios resource mapping")
@app_commands.autocomplete(model=product_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def removeheliosresource_cmd(interaction: discord.Interaction, model: str):
    resources = load_helios_resources()
    if model not in resources:
        await interaction.response.send_message(f"⚠️ No Helios mapping for **{model}**.", ephemeral=True)
        return
    del resources[model]
    save_helios_resources(resources)
    await interaction.response.send_message(f"🚫 Helios mapping removed for **{model}**.", ephemeral=True)
    await backup_config_to_discord("removeheliosresource")


@tree.command(name="heliosresources", description="List the model to Helios resource ID mappings")
@app_commands.checks.has_permissions(administrator=True)
async def heliosresources_cmd(interaction: discord.Interaction):
    resources = load_helios_resources()
    products = load_products()
    on_helios = [m for m in products if "helios" in products.get(m, {})]
    missing = [m for m in on_helios if m not in resources]

    embed = discord.Embed(title="🎯 Helios Resource Mapping", color=EMBED_COLOR)
    if resources:
        embed.description = "\n".join(
            f"● **{get_display_name(m)}** → `{rid}`" for m, rid in resources.items()
        )
    else:
        embed.description = "No mappings yet. Use `/setheliosresource`."

    if missing:
        embed.add_field(
            name="⚠️ Sold on Helios but unmapped",
            value="\n".join(f"● {get_display_name(m)} (`{m}`)" for m in missing)
                  + "\n\nThese fall back to the global `HELIOS_RESOURCE_ID`.",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ─────────────────────────────────────────────
#  VOUCH
# ─────────────────────────────────────────────

class VouchModal(discord.ui.Modal, title="Leave a Vouch"):
    text_input = discord.ui.TextInput(
        label="Your review",
        style=discord.TextStyle.paragraph,
        max_length=1000,
        required=True,
        placeholder="Tell us about your experience...",
    )

    def __init__(self, rating: int):
        super().__init__()
        self.rating = rating

    async def on_submit(self, interaction: discord.Interaction):
        channel = get_guild_channel(interaction.guild_id, "vouch_channel_id")
        if not channel:
            await interaction.response.send_message(
                "⚠️ Vouch channel not configured on this server. An admin can set it with `/setup`.",
                ephemeral=True,
            )
            return
        stars = "⭐" * self.rating + "☆" * (5 - self.rating)
        embed = discord.Embed(title=stars, description=str(self.text_input.value), color=0xF1C40F)
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        embed.set_footer(text=f"Rating: {self.rating}/5")
        await channel.send(embed=embed)
        await interaction.response.send_message("✅ Thank you for your feedback!", ephemeral=True)


class RatingSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=f"{'⭐' * i} ({i}/5)", value=str(i))
            for i in range(5, 0, -1)
        ]
        super().__init__(placeholder="Choose a rating...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(VouchModal(int(self.values[0])))


@tree.command(name="vouch", description="Leave a review")
async def vouch_cmd(interaction: discord.Interaction):
    view = discord.ui.View(timeout=120)
    view.add_item(RatingSelect())
    await interaction.response.send_message("How would you rate your experience?", view=view, ephemeral=True)


# ─────────────────────────────────────────────
#  FLASK (webhook Stripe)
# ─────────────────────────────────────────────

flask_app = Flask(__name__)


@flask_app.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({"error": "Invalid signature"}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        metadata = session.get("metadata", {})
        order_id = metadata.get("order_id")
        if order_id and main_loop:
            asyncio.run_coroutine_threadsafe(process_paid_order(order_id), main_loop)

    return jsonify({"status": "ok"}), 200


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)


# ─────────────────────────────────────────────
#  LANCEMENT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    main_loop.run_until_complete(bot.start(BOT_TOKEN))
