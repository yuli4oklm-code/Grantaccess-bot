"""
Bot Discord séparé : commande Stripe + livraison automatique sur Winsight via Playwright.
+ /weight system avec specs, tickets, et order flow
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
import uuid

import stripe
from flask import Flask, request, jsonify
from playwright.async_api import async_playwright

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

ENGINEX_USERNAME = os.environ.get("ENGINEX_USERNAME", "")
ENGINEX_PASSWORD = os.environ.get("ENGINEX_PASSWORD", "")
ENGINEX_LOGIN_URL = os.environ.get("ENGINEX_LOGIN_URL", "https://enginex-ex.com/auth/signin")
ENGINEX_ENTITLEMENTS_URL = os.environ.get("ENGINEX_ENTITLEMENTS_URL", "https://enginex-ex.com/entitlements")

STAFF_CHANNEL_ID = int(os.environ.get("STAFF_CHANNEL_ID", "0")) or None
ORDER_PANEL_CHANNEL_ID = int(os.environ.get("ORDER_PANEL_CHANNEL_ID", "0")) or None
VOUCH_CHANNEL_ID = int(os.environ.get("VOUCH_CHANNEL_ID", "0")) or None
TICKET_CATEGORY_ID = int(os.environ.get("TICKET_CATEGORY_ID", "1513173071916302499")) or None

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
ORDERS_FILE = os.path.join(DATA_DIR, "winsight_orders.json")
PRODUCTS_FILE = os.path.join(DATA_DIR, "winsight_products.json")
PIPELINES_FILE = os.path.join(DATA_DIR, "winsight_pipelines.json")
WEIGHTS_FILE = os.path.join(DATA_DIR, "weights.json")

EMBED_COLOR = 0x2F3136
CONFIG_BACKUP_MARKER = "WINSIGHT_BOT_CONFIG_BACKUP_V1"

DEFAULT_PRODUCTS = {
    "XyCubValorantV2": {"winsight": 2000},
}
DEFAULT_PIPELINES = {}

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


def load_weights():
    return load_json(WEIGHTS_FILE, {})


def save_weights(data):
    save_json(WEIGHTS_FILE, data)


def create_order(order_id, buyer_id, buyer_contact, model, platform, stripe_session_id):
    data = load_orders()
    data[order_id] = {
        "buyer_id": buyer_id,
        "buyer_contact": buyer_contact,
        "model": model,
        "platform": platform,
        "stripe_session_id": stripe_session_id,
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
    if not STAFF_CHANNEL_ID:
        return None
    channel = bot.get_channel(STAFF_CHANNEL_ID)
    if channel:
        return channel
    try:
        return await bot.fetch_channel(STAFF_CHANNEL_ID)
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
    print("[Config Backup] Restored products/pipelines from Discord backup.")
    return True


async def backup_config_to_discord(reason="manual update"):
    channel = await get_config_backup_channel()
    if not channel:
        print("[Config Backup] STAFF_CHANNEL_ID is not configured; backup skipped.")
        return
    payload = {
        "version": 1,
        "updated_at": int(time.time()),
        "reason": reason,
        "products": load_products(),
        "pipelines": load_pipelines(),
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
    specs_ex = weight_data.get("specs_ex", "")
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
    if specs_ex:
        embed.add_field(name="EX", value=specs_ex, inline=False)

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
        weight_name = weight_data.get("name", "Unknown")
        price_display = weight_data.get("price_display", "")

        # ── Préfixe du salon ticket ──
        prefix = weight_data.get("ticket_name_prefix", "").strip() or "ticket"
        safe_username = interaction.user.name.lower().replace(' ', '-')
        ticket_name = f"{prefix}-{safe_username}"

        # ── Catégorie : priorité à celle du weight, sinon TICKET_CATEGORY_ID global ──
        category = None
        custom_cat_id = weight_data.get("ticket_category_id", "").strip()
        if custom_cat_id:
            try:
                category = guild.get_channel(int(custom_cat_id))
            except ValueError:
                pass
        if category is None and TICKET_CATEGORY_ID:
            category = guild.get_channel(TICKET_CATEGORY_ID)

        # ── Vérifier ticket déjà ouvert ──
        existing = discord.utils.get(guild.text_channels, name=ticket_name)
        if existing:
            await interaction.response.send_message(
                f"❌ Tu as déjà un ticket ouvert : {existing.mention}", ephemeral=True
            )
            return

        # ── Permissions de base ──
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            interaction.user: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }

        # Admins toujours inclus
        for role in guild.roles:
            if role.permissions.administrator:
                overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        # Rôles staff personnalisés définis dans le weight
        staff_roles_raw = weight_data.get("ticket_staff_roles", "").strip()
        staff_role_objects = []
        if staff_roles_raw:
            for rid in staff_roles_raw.split(","):
                rid = rid.strip()
                if not rid:
                    continue
                try:
                    role = guild.get_role(int(rid))
                    if role:
                        overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
                        staff_role_objects.append(role)
                except ValueError:
                    pass

        # ── Créer le salon ──
        try:
            ticket_channel = await guild.create_text_channel(
                name=ticket_name,
                category=category,
                overwrites=overwrites,
                topic=f"Ticket — {weight_name} | {interaction.user.id}",
            )
        except Exception as e:
            await interaction.response.send_message(f"❌ Impossible de créer le ticket : {e}", ephemeral=True)
            return

        # ── Message d'accueil personnalisé ──
        welcome_template = weight_data.get("ticket_welcome_message", "").strip()
        if welcome_template:
            welcome_text = (
                welcome_template
                .replace("{user}", interaction.user.mention)
                .replace("{weight}", weight_name)
                .replace("{price}", price_display or "N/A")
            )
        else:
            welcome_text = (
                f"Bonjour {interaction.user.mention} ! 👋\n\n"
                f"Tu as ouvert un ticket pour **{weight_name}**.\n"
                f"{'**Prix :** ' + price_display + chr(10) if price_display else ''}"
                f"\nUn membre du staff va t'aider très bientôt.\n\n"
                f"*Tu peux aussi utiliser le bouton ci-dessous pour commander directement via Stripe.*"
            )

        ticket_embed = discord.Embed(
            title=f"🎫 {prefix.capitalize()} — {weight_name}",
            description=welcome_text,
            color=0x5865F2,
        )
        ticket_embed.set_footer(text=f"Ticket ouvert par {interaction.user} • {interaction.user.id}")

        # ── Mentions staff dans le ticket ──
        staff_mention_str = " ".join(r.mention for r in staff_role_objects) if staff_role_objects else ""
        open_content = f"{interaction.user.mention}"
        if staff_mention_str:
            open_content += f" {staff_mention_str}"

        close_view = TicketCloseView(interaction.user.id)
        await ticket_channel.send(content=open_content, embed=ticket_embed, view=close_view)

        # ── Notification staff ──
        if STAFF_CHANNEL_ID:
            staff_channel = bot.get_channel(STAFF_CHANNEL_ID)
            if staff_channel:
                await staff_channel.send(
                    f"📬 Nouveau ticket **{prefix}** pour **{weight_name}** "
                    f"par {interaction.user.mention} → {ticket_channel.mention}"
                )

        await interaction.response.send_message(
            f"✅ Ton ticket a été créé : {ticket_channel.mention}", ephemeral=True
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
            placeholder="image: https://...\nplatforms: ae,win,ex\nversion: v1.46.3\nauthor: swt\nmodel: XyCubValorantV2",
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
            # Specs vides par défaut
            "specs_general": "",
            "specs_ae": "",
            "specs_win": "",
            "specs_ex": "",
            # Config ticket vide par défaut
            "ticket_name_prefix": "",
            "ticket_category_id": "",
            "ticket_welcome_message": "",
            "ticket_staff_roles": "",
        }

        weights = load_weights()
        # Conserver les specs et config ticket existantes si on édite
        if self.weight_id and self.weight_id in weights:
            for key in ["specs_general", "specs_ae", "specs_win", "specs_ex",
                        "ticket_name_prefix", "ticket_category_id",
                        "ticket_welcome_message", "ticket_staff_roles"]:
                weight_data[key] = weights[self.weight_id].get(key, "")

        if not self.weight_id:
            # Générer un ID unique
            weight_id = re.sub(r'[^a-z0-9_]', '_', weight_data["name"].lower())
            weight_id = f"{weight_id}_{int(time.time())}"
        else:
            weight_id = self.weight_id

        weights[weight_id] = weight_data
        save_weights(weights)

        # Enchaîner directement sur le modal Specs (étape 2/3)
        await interaction.response.send_modal(
            WeightSpecsModal(weight_id=weight_id, existing=weight_data, step=2)
        )


class WeightSpecsModal(discord.ui.Modal, title="Step 2/3 - Specs"):

    def __init__(self, weight_id: str, existing: dict = None, step: int = 1):
        super().__init__()
        self.weight_id = weight_id
        self.step = step  # 1 = édition standalone, 2 = dans le flux création

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
        self.specs_ex = discord.ui.TextInput(
            label="EX specs",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=False,
            placeholder="• ...",
            default=existing.get("specs_ex", "") if existing else "",
        )

        self.add_item(self.specs_general)
        self.add_item(self.specs_ae)
        self.add_item(self.specs_win)
        self.add_item(self.specs_ex)

    async def on_submit(self, interaction: discord.Interaction):
        weights = load_weights()
        if self.weight_id not in weights:
            await interaction.response.send_message("❌ Weight not found.", ephemeral=True)
            return

        weights[self.weight_id]["specs_general"] = self.specs_general.value.strip()
        weights[self.weight_id]["specs_ae"] = self.specs_ae.value.strip()
        weights[self.weight_id]["specs_win"] = self.specs_win.value.strip()
        weights[self.weight_id]["specs_ex"] = self.specs_ex.value.strip()
        save_weights(weights)

        if self.step == 2:
            # Flux création → enchaîner sur la config ticket (étape 3/3)
            await interaction.response.send_modal(
                WeightTicketConfigModal(weight_id=self.weight_id, existing=weights[self.weight_id])
            )
        else:
            # Édition standalone → afficher le preview des specs
            embed = build_specs_embed(weights[self.weight_id])
            await interaction.response.send_message(
                f"✅ Specs mises à jour pour **{weights[self.weight_id]['name']}** !",
                embed=embed,
                ephemeral=True,
            )


class WeightTicketConfigModal(discord.ui.Modal, title="Step 3/3 - Ticket Config"):
    """Modal pour configurer la création de ticket liée à ce weight."""

    def __init__(self, weight_id: str, existing: dict = None):
        super().__init__()
        self.weight_id = weight_id

        self.ticket_prefix = discord.ui.TextInput(
            label="Préfixe du salon ticket",
            max_length=50,
            required=False,
            placeholder="purchase / support / order (défaut: ticket)",
            default=existing.get("ticket_name_prefix", "") if existing else "",
        )
        self.ticket_category = discord.ui.TextInput(
            label="ID de catégorie Discord (optionnel)",
            max_length=30,
            required=False,
            placeholder="Ex: 1234567890123456789 (laisser vide = défaut)",
            default=existing.get("ticket_category_id", "") if existing else "",
        )
        self.ticket_welcome = discord.ui.TextInput(
            label="Message d'accueil dans le ticket",
            style=discord.TextStyle.paragraph,
            max_length=800,
            required=False,
            placeholder=(
                "Bonjour {user} ! 👋\n"
                "Tu as ouvert un ticket pour **{weight}**.\n"
                "Un staff va t'aider rapidement.\n"
                "Variables dispo : {user} {weight} {price}"
            ),
            default=existing.get("ticket_welcome_message", "") if existing else "",
        )
        self.ticket_staff_roles = discord.ui.TextInput(
            label="IDs rôles staff à mentionner (séparés par des virgules)",
            max_length=300,
            required=False,
            placeholder="Ex: 111111111111111111, 222222222222222222",
            default=existing.get("ticket_staff_roles", "") if existing else "",
        )

        self.add_item(self.ticket_prefix)
        self.add_item(self.ticket_category)
        self.add_item(self.ticket_welcome)
        self.add_item(self.ticket_staff_roles)

    async def on_submit(self, interaction: discord.Interaction):
        weights = load_weights()
        if self.weight_id not in weights:
            await interaction.response.send_message("❌ Weight introuvable.", ephemeral=True)
            return

        weights[self.weight_id]["ticket_name_prefix"] = self.ticket_prefix.value.strip() or "ticket"
        weights[self.weight_id]["ticket_category_id"] = self.ticket_category.value.strip()
        weights[self.weight_id]["ticket_welcome_message"] = self.ticket_welcome.value.strip()
        weights[self.weight_id]["ticket_staff_roles"] = self.ticket_staff_roles.value.strip()
        save_weights(weights)

        weight_data = weights[self.weight_id]
        embed = build_weight_embed(weight_data)
        specs_embed = build_specs_embed(weight_data)
        view = WeightView(self.weight_id)

        # Résumé config ticket
        prefix = weight_data["ticket_name_prefix"]
        cat_id = weight_data["ticket_category_id"] or "défaut (TICKET_CATEGORY_ID)"
        roles_raw = weight_data["ticket_staff_roles"]
        roles_display = ", ".join(f"<@&{r.strip()}>" for r in roles_raw.split(",") if r.strip()) or "admins uniquement"

        ticket_info = (
            f"**Préfixe salon :** `{prefix}-<username>`\n"
            f"**Catégorie :** `{cat_id}`\n"
            f"**Rôles staff :** {roles_display}\n"
            f"**Message d'accueil :** {'✅ Personnalisé' if weight_data['ticket_welcome_message'] else '⚠️ Par défaut'}"
        )
        summary_embed = discord.Embed(
            title="✅ Weight créé avec succès !",
            description=ticket_info,
            color=0x57F287,
        )
        summary_embed.set_footer(text=f"ID: {self.weight_id} • Utilise /weight pour le poster")

        await interaction.response.send_message(
            embeds=[embed, specs_embed, summary_embed],
            view=view,
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


@tree.command(name="weight", description="Poster l'embed d'un weight dans le salon")
@app_commands.describe(weight_id="Le weight à poster")
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


@tree.command(name="weightadd", description="Ajouter ou modifier un weight (modal)")
@app_commands.describe(weight_id="Laisser vide pour créer, ou choisir un weight existant à modifier")
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


@tree.command(name="weightspecs", description="Éditer les specs d'un weight")
@app_commands.describe(weight_id="Le weight dont tu veux éditer les specs")
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


@tree.command(name="weightticket", description="Éditer la configuration ticket d'un weight")
@app_commands.describe(weight_id="Le weight dont tu veux modifier la config ticket")
@app_commands.autocomplete(weight_id=weight_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def weightticket_cmd(interaction: discord.Interaction, weight_id: str):
    weights = load_weights()
    weight_data = weights.get(weight_id)
    if not weight_data:
        await interaction.response.send_message("❌ Weight introuvable.", ephemeral=True)
        return
    modal = WeightTicketConfigModal(weight_id=weight_id, existing=weight_data)
    await interaction.response.send_modal(modal)


@tree.command(name="weightdelete", description="Supprimer un weight")
@app_commands.describe(weight_id="Le weight à supprimer")
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


@tree.command(name="weightlist", description="Lister tous les weights enregistrés")
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
        if platform == "enginex":
            label = "Your email (for EngineX)"
        else:
            label = "Your Discord ID (for Winsight)"
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

    # Ré-enregistrer les WeightViews persistantes
    weights = load_weights()
    for weight_id in weights:
        bot.add_view(WeightView(weight_id))

    print(f"✅ Bot connecté en tant que {bot.user} (ID: {bot.user.id})")

    if ORDER_PANEL_CHANNEL_ID:
        channel = bot.get_channel(ORDER_PANEL_CHANNEL_ID)
        if channel:
            embed = discord.Embed(title=ORDER_EMBED_TITLE, description=ORDER_EMBED_DESCRIPTION, color=EMBED_COLOR)
            await channel.send(embed=embed, view=OrderStartView())
            print(f"📌 Order panel posted in #{channel.name}")


# ─────────────────────────────────────────────
#  COMMANDES ADMIN EXISTANTES
# ─────────────────────────────────────────────

@tree.command(name="order", description="Poster le panneau de commande Stripe")
@app_commands.checks.has_permissions(administrator=True)
async def order_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title=ORDER_EMBED_TITLE, description=ORDER_EMBED_DESCRIPTION, color=EMBED_COLOR)
    await interaction.channel.send(embed=embed, view=OrderStartView())
    await interaction.response.send_message("✅ Order panel posted!", ephemeral=True)


platform_choices = [
    app_commands.Choice(name="Winsight", value="winsight"),
    app_commands.Choice(name="EngineX", value="enginex"),
]


@tree.command(name="addproduct", description="Ajouter ou mettre à jour un modèle/plateforme en vente")
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


@tree.command(name="removeproduct", description="Retirer un modèle (ou une plateforme précise) de la vente")
@app_commands.describe(platform="Laisse vide pour retirer le modèle de TOUTES les plateformes")
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


@tree.command(name="productlist", description="Afficher la liste des modèles disponibles")
@app_commands.describe(public="Poster publiquement dans le salon (par défaut: visible que pour toi)")
async def productlist_cmd(interaction: discord.Interaction, public: bool = False):
    embed = build_product_list_embed()
    await interaction.response.send_message(embed=embed, ephemeral=not public)


# ─────────────────────────────────────────────
#  AUTOMATISATION WINSIGHT (Playwright)
# ─────────────────────────────────────────────

async def _winsight_login(page) -> bool:
    """Ouvre Winsight et se connecte si nécessaire. Retourne True si connecté."""
    await page.goto(WINSIGHT_URL, timeout=30000)
    await page.wait_for_load_state("networkidle", timeout=30000)
    login_input = await page.query_selector("input[type='text']")
    if login_input:
        await page.fill("input[type='text']", WINSIGHT_USERNAME)
        await page.fill("input[type='password']", WINSIGHT_PASSWORD)
        clicked = await page.evaluate("""
            () => {
                const buttons = document.querySelectorAll("button");
                for (const btn of buttons) {
                    if (btn.textContent.toUpperCase().includes("SIGN IN")) { btn.click(); return true; }
                }
                return false;
            }
        """)
        if not clicked:
            await page.click("text=SIGN IN", timeout=10000)
        await asyncio.sleep(3)
        await page.wait_for_load_state("networkidle", timeout=30000)
    return True


async def _winsight_grant_one(page, discord_id: str, model_name: str) -> tuple[bool, str]:
    """Grant un seul modèle sur une page déjà connectée. Recherche insensible à la casse."""
    match_info = await page.evaluate(f"""
        () => {{
            // Reset previous markers
            document.querySelectorAll("[data-bot-target-input]").forEach(el => el.removeAttribute("data-bot-target-input"));
            document.querySelectorAll("[data-bot-target-button]").forEach(el => el.removeAttribute("data-bot-target-button"));

            const modelName = "{model_name}".toLowerCase();
            const allElements = document.querySelectorAll("*");
            let matchEl = null;
            for (const el of allElements) {{
                let directText = "";
                for (const node of el.childNodes) {{
                    if (node.nodeType === Node.TEXT_NODE) directText += node.textContent;
                }}
                // Comparaison insensible à la casse ET aux underscores/tirets
                const normalized = directText.toLowerCase().replace(/[_\\-]/g, "");
                const searchNorm = modelName.replace(/[_\\-]/g, "");
                if (normalized.includes(searchNorm)) {{ matchEl = el; break; }}
            }}
            if (!matchEl) return {{ status: "not_found" }};
            let parent = matchEl;
            for (let i = 0; i < 12; i++) {{
                parent = parent.parentElement;
                if (!parent) break;
                const input = parent.querySelector("input[placeholder*='username'], input[placeholder*='customer'], input[placeholder*='Username']");
                const buttons = parent.querySelectorAll("button");
                let shareBtn = null;
                for (const btn of buttons) {{
                    if (btn.textContent.toUpperCase().includes("SHARE")) {{ shareBtn = btn; break; }}
                }}
                if (input && shareBtn) {{
                    input.setAttribute("data-bot-target-input", "true");
                    shareBtn.setAttribute("data-bot-target-button", "true");
                    return {{ status: "found", matchedText: matchEl.textContent.trim().substring(0, 100) }};
                }}
            }}
            return {{ status: "container_not_found", matchedText: matchEl.textContent.trim().substring(0, 100) }};
        }}
    """)

    if match_info["status"] == "found":
        input_locator = page.locator("[data-bot-target-input='true']")
        await input_locator.click()
        await input_locator.fill("")
        await input_locator.type(discord_id, delay=30)
        await asyncio.sleep(0.5)
        share_button = page.locator("[data-bot-target-button='true']")
        await share_button.click()
        await asyncio.sleep(1.5)
        return True, f"✓ {model_name}"
    elif match_info["status"] == "container_not_found":
        return False, f"✗ {model_name} (trouvé mais pas d'input/share button)"
    else:
        return False, f"✗ {model_name} (introuvable sur la page)"


async def winsight_grant(discord_id: str, model_name: str) -> tuple[bool, str]:
    """Grant un seul modèle (ouvre et ferme son propre browser)."""
    print(f"[Winsight] Grant: discord_id={discord_id}, model={model_name}")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await _winsight_login(page)
            success, msg = await _winsight_grant_one(page, discord_id, model_name)
            await browser.close()
            if success:
                return True, f"Access granted to {discord_id} for {model_name} on Winsight."
            return False, msg
    except Exception as e:
        return False, f"Error: {str(e)}"


async def _winsight_scrape_matching(page, keyword: str) -> list[str]:
    """
    Scrape la page Winsight connectée et retourne les noms exacts de toutes les
    weights dont le nom contient `keyword` (insensible à la casse).
    Cherche les éléments qui ont un input 'username/customer' + bouton 'SHARE' à proximité.
    """
    found_names = await page.evaluate(f"""
        () => {{
            const keyword = "{keyword}".toLowerCase();
            const results = [];

            // Cherche tous les éléments dont le texte direct contient le keyword
            const allElements = document.querySelectorAll("*");
            for (const el of allElements) {{
                let directText = "";
                for (const node of el.childNodes) {{
                    if (node.nodeType === Node.TEXT_NODE) directText += node.textContent;
                }}
                const normalized = directText.toLowerCase().replace(/[_\\-\\s]/g, "");
                const keyNorm   = keyword.replace(/[_\\-\\s]/g, "");
                if (!normalized.includes(keyNorm)) continue;

                // Vérifie que ce nœud est bien la "card" d'une weight
                // (un parent proche contient un input username + bouton SHARE)
                let parent = el;
                for (let i = 0; i < 12; i++) {{
                    parent = parent.parentElement;
                    if (!parent) break;
                    const input = parent.querySelector(
                        "input[placeholder*='username'], input[placeholder*='customer'], input[placeholder*='Username']"
                    );
                    const buttons = parent.querySelectorAll("button");
                    let hasShare = false;
                    for (const btn of buttons) {{
                        if (btn.textContent.toUpperCase().includes("SHARE")) {{ hasShare = true; break; }}
                    }}
                    if (input && hasShare) {{
                        const name = directText.trim();
                        if (name && !results.includes(name)) results.push(name);
                        break;
                    }}
                }}
            }}
            return results;
        }}
    """)
    print(f"[Winsight Scrape] keyword='{keyword}' → found: {found_names}")
    return found_names


async def winsight_grant_all_dynamic(discord_id: str, keyword: str) -> tuple[int, int, list[str]]:
    """
    Ouvre Winsight, scrape dynamiquement toutes les weights dont le nom contient
    `keyword`, puis les grant toutes en une seule session browser.
    Retourne (nb_success, nb_fail, detail_lines).
    """
    print(f"[Winsight] Grant ALL dynamic: discord_id={discord_id}, keyword='{keyword}'")
    successes = 0
    failures = 0
    details = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await _winsight_login(page)

            # Scrape les noms réels sur la page
            matching_names = await _winsight_scrape_matching(page, keyword)

            if not matching_names:
                await browser.close()
                return 0, 0, [f"⚠️ Aucune weight contenant « {keyword} » trouvée sur Winsight."]

            # Grant chacune dans la même session
            for name in matching_names:
                ok, msg = await _winsight_grant_one(page, discord_id, name)
                if ok:
                    successes += 1
                else:
                    failures += 1
                details.append(msg)

            await browser.close()
    except Exception as e:
        details.append(f"Browser error: {str(e)}")
        failures = max(1, failures)
    return successes, failures, details


async def winsight_grant_all(discord_id: str, model_names: list[str]) -> tuple[int, int, list[str]]:
    """
    Grant une liste fixe de modèles en une seule session browser.
    Utilisé pour les grants via Stripe (liste connue à l'avance).
    """
    print(f"[Winsight] Grant ALL (fixed list): discord_id={discord_id}, models={model_names}")
    successes = 0
    failures = 0
    details = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await _winsight_login(page)
            for model_name in model_names:
                ok, msg = await _winsight_grant_one(page, discord_id, model_name)
                if ok:
                    successes += 1
                else:
                    failures += 1
                details.append(msg)
            await browser.close()
    except Exception as e:
        details.append(f"Browser error: {str(e)}")
        failures += len(model_names) - successes
    return successes, failures, details


async def winsight_grant_pipeline(discord_id: str, pipeline_site_name: str) -> tuple[bool, str]:
    success, message = await winsight_grant(discord_id, pipeline_site_name)
    if success:
        return True, f"Pipeline access granted to {discord_id} for {pipeline_site_name} on Winsight."
    return False, message


async def enginex_grant(email: str, model_name: str) -> tuple[bool, str]:
    print(f"[EngineX] Starting grant for email={email}, model={model_name}")
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(ENGINEX_LOGIN_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page.locator("input[type='email'], input[name='email']").first.fill(ENGINEX_USERNAME)
            await page.locator("input[type='password']").first.fill(ENGINEX_PASSWORD)
            clicked = await page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll("button");
                    for (const btn of buttons) {
                        if (btn.textContent.toUpperCase().includes("SIGN IN")) { btn.click(); return true; }
                    }
                    return false;
                }
            """)
            if not clicked:
                await page.click("text=Sign in", timeout=10000)
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page.goto(ENGINEX_ENTITLEMENTS_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)

            grant_clicked = await page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll("button");
                    for (const btn of buttons) {
                        if (btn.textContent.toUpperCase().includes("GRANT ACCESS")) { btn.click(); return true; }
                    }
                    return false;
                }
            """)
            if not grant_clicked:
                await browser.close()
                return False, "Could not find 'Grant Access' button."

            await asyncio.sleep(1)
            search_input = page.locator("input[placeholder*='email'], input[placeholder*='Discord'], input[placeholder*='username']").first
            await search_input.click()
            await search_input.fill("")
            await search_input.type(email, delay=30)
            await asyncio.sleep(1.5)

            result_clicked = False
            try:
                result_locator = page.locator("div[style*='cursor: pointer']").first
                await result_locator.click(timeout=5000)
                result_clicked = True
            except Exception:
                try:
                    result_locator = page.locator(f"*:not(input):has-text('{email}')").last
                    parent_locator = result_locator.locator("xpath=..")
                    await parent_locator.click(timeout=5000)
                    result_clicked = True
                except Exception:
                    pass

            if not result_clicked:
                await browser.close()
                return False, f"Could not find user matching email '{email}'."

            await asyncio.sleep(0.8)
            model_label = await page.evaluate(f"""
                () => {{
                    const modelName = "{model_name}".toLowerCase();
                    const selects = document.querySelectorAll("select");
                    for (const select of selects) {{
                        for (const option of select.options) {{
                            if (option.textContent.toLowerCase().includes(modelName)) return option.textContent;
                        }}
                    }}
                    return null;
                }}
            """)

            model_selected = False
            if model_label:
                try:
                    await page.locator("select").first.select_option(label=model_label)
                    model_selected = True
                except Exception:
                    pass

            if not model_selected:
                await browser.close()
                return False, f"Could not find model '{model_name}' in dropdown."

            await asyncio.sleep(0.5)
            final_clicked = await page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll("button");
                    for (const btn of buttons) {
                        if (btn.textContent.trim().toUpperCase() === "GRANT ACCESS") { btn.click(); return true; }
                    }
                    return false;
                }
            """)
            await asyncio.sleep(2)
            modal_still_open = await page.evaluate("""
                () => {
                    const body = document.body.innerText;
                    return body.includes("Grant Model Access") && body.includes("Select a user and a model");
                }
            """)
            await browser.close()

            if final_clicked and not modal_still_open:
                return True, f"Access granted to {email} for {model_name} on EngineX."
            elif final_clicked:
                return False, "Clicked Grant Access but modal is still open."
            else:
                return False, "Could not click final 'Grant Access' button."
    except Exception as e:
        return False, f"Error: {str(e)}"


async def process_paid_order(order_id: str):
    order = get_order(order_id)
    if not order:
        return
    update_order(order_id, status="processing")
    platform = order.get("platform", "winsight")

    if platform == "enginex":
        success, message = await enginex_grant(order["buyer_contact"], order["model"])
    elif platform == "winsight_dynamic":
        nb_ok, nb_fail, details = await winsight_grant_all_dynamic(order["buyer_contact"], order["model"])
        success = nb_ok > 0 and nb_fail == 0
        message = (
            f"Dynamic Winsight grant for keyword '{order['model']}': "
            f"{nb_ok} added, {nb_fail} failed.\n" + "\n".join(details)
        )
    else:
        success, message = await winsight_grant(order["buyer_contact"], order["model"])

    update_order(order_id, status="delivered" if success else "failed")

    if STAFF_CHANNEL_ID:
        channel = bot.get_channel(STAFF_CHANNEL_ID)
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
#  VÉRIFICATION / REVOKE D'ACCÈS
# ─────────────────────────────────────────────

async def winsight_check(discord_id: str, model_name: str) -> tuple[bool, str]:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(WINSIGHT_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            login_input = await page.query_selector("input[type='text']")
            if login_input:
                await page.fill("input[type='text']", WINSIGHT_USERNAME)
                await page.fill("input[type='password']", WINSIGHT_PASSWORD)
                await page.evaluate("""
                    () => {
                        const buttons = document.querySelectorAll("button");
                        for (const btn of buttons) {
                            if (btn.textContent.toUpperCase().includes("SIGN IN")) { btn.click(); return true; }
                        }
                    }
                """)
                await asyncio.sleep(3)
                await page.wait_for_load_state("networkidle", timeout=30000)

            has_access = await page.evaluate(f"""
                () => {{
                    const modelName = "{model_name}".toLowerCase();
                    const discordId = "{discord_id}";
                    const allElements = document.querySelectorAll("*");
                    let matchEl = null;
                    for (const el of allElements) {{
                        let directText = "";
                        for (const node of el.childNodes) {{
                            if (node.nodeType === Node.TEXT_NODE) directText += node.textContent;
                        }}
                        if (directText.toLowerCase().includes(modelName)) {{ matchEl = el; break; }}
                    }}
                    if (!matchEl) return null;
                    let parent = matchEl;
                    for (let i = 0; i < 12; i++) {{
                        parent = parent.parentElement;
                        if (!parent) break;
                        if (parent.textContent.includes(discordId)) return true;
                        const input = parent.querySelector("input[placeholder*='username'], input[placeholder*='customer']");
                        if (input) return false;
                    }}
                    return null;
                }}
            """)
            await browser.close()

            if has_access is True:
                return True, f"✅ {discord_id} has access to **{model_name}** on Winsight."
            elif has_access is False:
                return False, f"❌ {discord_id} does NOT have access to **{model_name}** on Winsight."
            else:
                return False, "⚠️ Could not determine access."
    except Exception as e:
        return False, f"Error: {str(e)}"


async def winsight_revoke(discord_id: str, model_name: str) -> tuple[bool, str]:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(WINSIGHT_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            login_input = await page.query_selector("input[type='text']")
            if login_input:
                await page.fill("input[type='text']", WINSIGHT_USERNAME)
                await page.fill("input[type='password']", WINSIGHT_PASSWORD)
                await page.evaluate("""
                    () => {
                        const buttons = document.querySelectorAll("button");
                        for (const btn of buttons) {
                            if (btn.textContent.toUpperCase().includes("SIGN IN")) { btn.click(); return true; }
                        }
                    }
                """)
                await asyncio.sleep(3)
                await page.wait_for_load_state("networkidle", timeout=30000)

            try:
                title_el = page.locator(f"*:has-text('{model_name}')").last
                model_card = title_el
                chip_locator = None
                count = 0
                for _ in range(8):
                    chip_locator = model_card.locator(f"[data-testid*='badge-share'][data-testid*='{discord_id}']")
                    count = await chip_locator.count()
                    if count > 0:
                        break
                    model_card = model_card.locator("xpath=..")

                if count == 0:
                    await browser.close()
                    return False, f"⚠️ {discord_id} doesn't appear to have access to **{model_name}**."

                await chip_locator.first.click(timeout=5000)
                result = "clicked"
            except Exception as e:
                result = "click_failed"

            await asyncio.sleep(2)
            await browser.close()

            if result == "clicked":
                return True, f"✅ Access revoked for {discord_id} on **{model_name}**."
            else:
                return False, "❌ Found chip but couldn't click remove."
    except Exception as e:
        return False, f"Error: {str(e)}"


async def enginex_check(email: str, model_name: str) -> tuple[bool, str]:
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(ENGINEX_LOGIN_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page.locator("input[type='email'], input[name='email']").first.fill(ENGINEX_USERNAME)
            await page.locator("input[type='password']").first.fill(ENGINEX_PASSWORD)
            await page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll("button");
                    for (const btn of buttons) {
                        if (btn.textContent.toUpperCase().includes("SIGN IN")) { btn.click(); return true; }
                    }
                }
            """)
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page.goto(ENGINEX_ENTITLEMENTS_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)

            has_access = await page.evaluate(f"""
                () => {{
                    const emailLower = "{email}".toLowerCase();
                    const modelName = "{model_name}".toLowerCase();
                    const rows = document.querySelectorAll("tr, [class*='row']");
                    for (const row of rows) {{
                        if (row.textContent.toLowerCase().includes(emailLower)) {{
                            return row.textContent.toLowerCase().includes(modelName);
                        }}
                    }}
                    return null;
                }}
            """)
            await browser.close()

            if has_access is True:
                return True, f"✅ {email} has access to **{model_name}** on EngineX."
            elif has_access is False:
                return False, f"❌ {email} does NOT have access to **{model_name}** on EngineX."
            else:
                return False, f"⚠️ Could not find {email} in entitlements."
    except Exception as e:
        return False, f"Error: {str(e)}"


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


@tree.command(name="setpipeline", description="Enregistrer le nom exact d'une pipeline sur Winsight")
@app_commands.checks.has_permissions(administrator=True)
async def setpipeline_cmd(interaction: discord.Interaction, pipeline: str, site_name: str):
    pipelines = load_pipelines()
    pipelines[pipeline] = site_name
    save_pipelines(pipelines)
    await interaction.response.send_message(
        f"✅ Pipeline **{pipeline}** → `{site_name}`.", ephemeral=False
    )
    await backup_config_to_discord("setpipeline")


@tree.command(name="removepipeline", description="Supprimer une pipeline enregistrée")
@app_commands.autocomplete(pipeline=pipeline_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def removepipeline_cmd(interaction: discord.Interaction, pipeline: str):
    pipelines = load_pipelines()
    if pipeline not in pipelines:
        await interaction.response.send_message(f"⚠️ Pipeline **{pipeline}** introuvable.", ephemeral=False)
        return
    del pipelines[pipeline]
    save_pipelines(pipelines)
    await interaction.response.send_message(f"🚫 Pipeline **{pipeline}** supprimée.", ephemeral=False)
    await backup_config_to_discord("removepipeline")


@tree.command(name="pipelinelist", description="Afficher les pipelines enregistrées")
@app_commands.checks.has_permissions(administrator=True)
async def pipelinelist_cmd(interaction: discord.Interaction):
    pipelines = load_pipelines()
    if not pipelines:
        await interaction.response.send_message("Aucune pipeline enregistrée.", ephemeral=False)
        return
    lines = [f"● **{name}** → `{site_name}`" for name, site_name in pipelines.items()]
    embed = discord.Embed(title="Pipelines Winsight", description="\n".join(lines), color=EMBED_COLOR)
    await interaction.response.send_message(embed=embed, ephemeral=False)


@tree.command(name="pipelineadd", description="Ajouter une pipeline Winsight à un utilisateur")
@app_commands.autocomplete(pipeline=pipeline_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def pipelineadd_cmd(interaction: discord.Interaction, pipeline: str, discord_id: str):
    pipelines = load_pipelines()
    if pipeline not in pipelines:
        await interaction.response.send_message(
            f"❌ Pipeline **{pipeline}** introuvable.", ephemeral=False
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


@tree.command(name="grantaccess", description="Donner manuellement l'accès à un jeu (toutes versions d'un coup)")
@app_commands.describe(
    model="Le jeu à donner (ex: Valorant → grant toutes les versions Valorant)",
    platform="La plateforme",
    contact="ID Discord (Winsight) ou email (EngineX)",
)
@app_commands.autocomplete(model=model_autocomplete)
@app_commands.choices(platform=platform_choices)
@app_commands.checks.has_permissions(administrator=True)
async def grantaccess_cmd(interaction: discord.Interaction, model: str, platform: app_commands.Choice[str], contact: str):
    # model est le nom de groupe (ex: "Valorant") — sert de keyword de recherche
    group_name = model
    available_platforms = get_platforms_for_group(group_name)

    if platform.value not in available_platforms:
        await interaction.response.send_message(
            f"❌ **{group_name}** not configured on **{platform.name}**. "
            f"Available: {', '.join(available_platforms) or 'none'}",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"⏳ Searching Winsight for all weights containing **{group_name}** "
        f"and granting to `{contact}`...",
        ephemeral=False,
    )

    async def run():
        if platform.value == "enginex":
            # EngineX : liste fixe depuis products.json (pas de scraping live)
            all_models = get_models_for_group(group_name)
            products = load_products()
            models_on_platform = [m for m in all_models if platform.value in products.get(m, {})]
            results = []
            total_ok = 0
            for m in models_on_platform:
                ok, msg = await enginex_grant(contact, m)
                results.append(f"{'✓' if ok else '✗'} {get_display_name(m)}")
                if ok:
                    total_ok += 1
            nb_ok = total_ok
            nb_fail = len(models_on_platform) - total_ok
            details = results
        else:
            # Winsight : scraping dynamique — trouve toutes les weights
            # dont le nom contient group_name sur la page, peu importe la version
            nb_ok, nb_fail, details = await winsight_grant_all_dynamic(contact, group_name)

        all_ok = nb_fail == 0
        color = 0x57F287 if all_ok else (0xF1C40F if nb_ok > 0 else 0xED4245)
        prefix = "WIN" if platform.value == "winsight" else "EX"

        if all_ok:
            title = f"✅ {prefix} Granted — {group_name}"
        elif nb_ok > 0:
            title = f"⚠️ {prefix} Partial Grant — {group_name}"
        else:
            title = f"❌ {prefix} Grant Failed — {group_name}"

        embed = discord.Embed(title=title, color=color)

        if platform.value == "winsight":
            embed.add_field(name="User", value=f"<@{contact}> ({contact})", inline=False)
        else:
            embed.add_field(name="EngineX Email", value=contact, inline=False)

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


@tree.command(name="checkaccess", description="Vérifier si un client a accès à un jeu (toutes versions)")
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

    all_models = get_models_for_group(group_name)
    products = load_products()
    models_on_platform = [m for m in all_models if platform.value in products.get(m, {})]

    await interaction.response.send_message(
        f"⏳ Checking **{group_name}** ({len(models_on_platform)} version(s)) for `{contact}`...", ephemeral=True
    )

    async def run():
        lines = []
        for m in models_on_platform:
            if platform.value == "enginex":
                _, msg = await enginex_check(contact, m)
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


@tree.command(name="revoke", description="Retirer l'accès d'un client (Winsight uniquement, toutes versions)")
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
        if not VOUCH_CHANNEL_ID:
            await interaction.response.send_message("⚠️ Vouch channel not configured.", ephemeral=True)
            return
        channel = bot.get_channel(VOUCH_CHANNEL_ID)
        if not channel:
            await interaction.response.send_message("⚠️ Could not find vouch channel.", ephemeral=True)
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


@tree.command(name="vouch", description="Laisser un témoignage")
async def vouch_cmd(interaction: discord.Interaction):
    view = discord.ui.View(timeout=120)
    view.add_item(RatingSelect())
    await interaction.response.send_message("How would you rate your experience?", view=view, ephemeral=True)


# ─────────────────────────────────────────────
#  FLASK (webhook Stripe)
# ─────────────────────────────────────────────

flask_app = Flask(__name__)


SITE_ORIGIN = os.environ.get("SITE_ORIGIN", "https://cubismael.com")
SITE_SUCCESS_URL = os.environ.get("SITE_SUCCESS_URL", "https://cubismael.com/?payment=success")
SITE_CANCEL_URL = os.environ.get("SITE_CANCEL_URL", "https://cubismael.com/?payment=cancel")

SITE_PRODUCTS = {
    "weight-valorant": {
        "name": "Valorant Weight",
        "model": "XyCubValorantV2",
        "platform": "winsight",
        "price_cents": 3000,
    },
    "weight-marvel-rivals": {
        "name": "Marvel Rivals Weight",
        "model": os.environ.get("MARVEL_RIVALS_KEYWORD", "Marvels"),
        "platform": "winsight_dynamic",
        "price_cents": 3000,
    },
}


def site_checkout_response(payload, status=200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Access-Control-Allow-Origin"] = SITE_ORIGIN
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response


@flask_app.route("/site-create-checkout", methods=["OPTIONS", "POST"])
def site_create_checkout():
    if request.method == "OPTIONS":
        return site_checkout_response({})

    data = request.get_json(force=True, silent=True) or {}
    product_id = str(data.get("productId", "")).strip()
    buyer_contact = str(data.get("buyerContact", "")).strip()
    product = SITE_PRODUCTS.get(product_id)

    if not product:
        return site_checkout_response({"error": "Unknown product"}, 400)
    if len(buyer_contact) < 3 or len(buyer_contact) > 120:
        return site_checkout_response({"error": "Enter a valid Discord username or Winsight email"}, 400)

    order_id = f"site_{int(time.time())}_{uuid.uuid4().hex[:8]}"

    checkout_session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[
            {
                "price_data": {
                    "currency": "eur",
                    "product_data": {"name": product["name"]},
                    "unit_amount": product["price_cents"],
                },
                "quantity": 1,
            }
        ],
        mode="payment",
        success_url=SITE_SUCCESS_URL,
        cancel_url=SITE_CANCEL_URL,
        metadata={
            "order_id": order_id,
            "source": "website",
            "product_id": product_id,
        },
    )

    create_order(
        order_id=order_id,
        buyer_id=0,
        buyer_contact=buyer_contact,
        model=product["model"],
        platform=product["platform"],
        stripe_session_id=checkout_session.id,
    )

    return site_checkout_response({"url": checkout_session.url})


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
