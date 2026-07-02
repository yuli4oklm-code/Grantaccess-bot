import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import json
import time

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────

BOT_TOKEN = os.environ["BOT_TOKEN"]

TICKET_CHANNEL_PREFIXES = ["ticket", "weights", "support"]
TICKET_CATEGORY_ID = None
EMBED_COLOR = 0x2F3136
REVIEW_CHANNEL_ID = 1491186873819992085
DONE_REVIEW_MESSAGE = (
    f"✅ All done! Thanks for choosing us. If everything went well, "
    f"please leave a review in <#{REVIEW_CHANNEL_ID}> — it helps us a lot!"
)

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)

TEMPLATES_FILE = os.path.join(DATA_DIR, "templates.json")
KEYWORDS_FILE = os.path.join(DATA_DIR, "keywords.json")
CUSTOM_COMMANDS_FILE = os.path.join(DATA_DIR, "custom_commands.json")
STICKY_FILE = os.path.join(DATA_DIR, "sticky.json")
GIVEAWAYS_FILE = os.path.join(DATA_DIR, "giveaways.json")
ORDERS_FILE = os.path.join(DATA_DIR, "orders.json")
BOT_STATE_FILE = os.path.join(DATA_DIR, "bot_state.json")
REMINDERS_FILE = os.path.join(DATA_DIR, "reminders.json")

PAYPAL_LINK = os.environ.get("PAYPAL_LINK", "https://paypal.me/yourusername")
STAFF_ORDER_CHANNEL_ID = int(os.environ.get("STAFF_ORDER_CHANNEL_ID", "0")) or None
ORDER_PANEL_CHANNEL_ID = int(os.environ.get("ORDER_PANEL_CHANNEL_ID", "0")) or None
CREDIT_LINE = "© Cubis Services — All Rights Reserved"

# ─────────────────────────────────────────────
#  TEMPLATES PAR DÉFAUT
# ─────────────────────────────────────────────

DEFAULT_TEMPLATES = {
    "normal": {
        "title": "🎫 New Ticket Opened",
        "description": (
            "Hey! Thank you for reaching out. Our team will be with you shortly.\n\n"
            "Before one of our experts jumps in, please take a moment to fill out the information below:\n\n"
            "● **Game** — Which game do you need setup or help with?\n"
            "● **Software** — Aim Engine / Aim Forge / Winsight / Engine X / None\n"
            "● **GPU** — e.g. RTX 3070 / 4070 / 5070 / Not Sure\n"
            "● **Capture Card** — Elgato 4KX / Elgato HD60 / Elgato Pro / None / Not Sure\n"
            "● **Setup** — Dual PC / Xbox / PlayStation?\n"
            "● **Input** — Controller or XIM/MNK?\n"
            "● **Monitor Refresh Rate** — 60Hz / 120Hz / 144Hz / Not Sure\n\n"
            "Can't answer something? No worries, just let us know and we'll help you figure it out!"
        ),
        "thumbnail_url": "",
        "image_url": "",
    },
    "weights": {
        "title": "🎯 Weights Request",
        "description": (
            "Hey! Thanks for opening a ticket for weights.\n\n"
            "Please answer the following questions so we can process your request:\n\n"
            "● **Software** — What software do you need the weights on?\n"
            "● **Weights** — What weights are you looking for?\n"
            "● **Payment Method** — What payment method will you be using?\n"
            "● **TOS** — Have you read our TOS?\n\n"
            "A member of our team will assist you shortly."
        ),
        "thumbnail_url": "",
        "image_url": "",
    },
    "support": {
        "title": "🛠️ Support Ticket",
        "description": (
            "Hey! Thanks for reaching out to support.\n\n"
            "Please describe your issue in as much detail as possible, and a team member will assist you shortly.\n\n"
            "● **Issue** — What's going wrong?\n"
            "● **Since when** — When did this start happening?\n"
            "● **Steps taken** — What have you already tried?\n\n"
            "We'll get back to you as soon as possible!"
        ),
        "thumbnail_url": "",
        "image_url": "",
    },
}

# ─────────────────────────────────────────────
#  GESTION DES TEMPLATES (stockage persistant)
# ─────────────────────────────────────────────

def load_templates():
    if os.path.exists(TEMPLATES_FILE):
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # S'assurer que toutes les clés existent (rétrocompatibilité)
        for kind, defaults in DEFAULT_TEMPLATES.items():
            if kind not in data:
                data[kind] = defaults
            else:
                for key, val in defaults.items():
                    data[kind].setdefault(key, val)
        return data
    return json.loads(json.dumps(DEFAULT_TEMPLATES))


def save_templates(data):
    with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def set_template(kind: str, title: str, description: str, thumbnail_url: str = "", image_url: str = ""):
    data = load_templates()
    data[kind] = {
        "title": title,
        "description": description,
        "thumbnail_url": thumbnail_url,
        "image_url": image_url,
    }
    save_templates(data)


def get_template(kind: str):
    data = load_templates()
    return data.get(kind, DEFAULT_TEMPLATES[kind])


def is_valid_url(url: str) -> bool:
    return url.strip() == "" or url.strip().startswith("http://") or url.strip().startswith("https://")


def parse_emoji_input(raw: str):
    """Convertit une entrée utilisateur (unicode ou :nom:id>) en quelque chose d'utilisable par discord.ui.Button."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("<") and raw.endswith(">"):
        try:
            return discord.PartialEmoji.from_str(raw)
        except Exception:
            return None
    return raw  # emoji unicode standard, ou nom simple sans <> (sera ignoré silencieusement si invalide)


# ─────────────────────────────────────────────
#  GESTION DES MOTS-CLÉS (auto-réponses)
# ─────────────────────────────────────────────
# Structure d'une entrée :
# {
#   "keyword": "refund",
#   "response_type": "text" | "embed",
#   "text": "...",                  (si text)
#   "embed_title": "...",           (si embed)
#   "embed_description": "...",     (si embed)
#   "scope": "all" | "normal" | "weights" | "support"
# }

def load_keywords():
    if os.path.exists(KEYWORDS_FILE):
        with open(KEYWORDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_keywords(data):
    with open(KEYWORDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def add_keyword(entry: dict):
    data = load_keywords()
    # Remplace si le même mot-clé existe déjà pour le même salon OU le même scope
    def is_duplicate(k):
        same_keyword = k["keyword"].lower() == entry["keyword"].lower()
        if entry.get("channel_id"):
            return same_keyword and k.get("channel_id") == entry.get("channel_id")
        return same_keyword and k.get("scope") == entry.get("scope")

    data = [k for k in data if not is_duplicate(k)]
    data.append(entry)
    save_keywords(data)


def remove_keyword(keyword: str) -> bool:
    data = load_keywords()
    new_data = [k for k in data if k["keyword"].lower() != keyword.lower()]
    if len(new_data) == len(data):
        return False
    save_keywords(new_data)
    return True


def find_matching_keywords(message: discord.Message, ticket_kind: str = None):
    """Retourne la liste des entrées mot-clé qui matchent le message, son salon, et/ou son scope ticket."""
    data = load_keywords()
    content_lower = message.content.lower()
    matches = []
    for entry in data:
        if entry["keyword"].lower() not in content_lower:
            continue

        channel_id = entry.get("channel_id")
        scope = entry.get("scope")

        if channel_id:
            # Mot-clé lié à un salon précis
            if message.channel.id == channel_id:
                matches.append(entry)
        elif scope and ticket_kind is not None:
            # Mot-clé lié à un scope de ticket (ancien système)
            if scope == "all" or scope == ticket_kind:
                matches.append(entry)
    return matches

# ─────────────────────────────────────────────
#  COMMANDES PERSONNALISÉES (/commandcreate)
# ─────────────────────────────────────────────
# Structure d'une commande personnalisée, indexée par "prefix+name" (ex: "!rules") :
# {
#   "prefix": "!", "name": "rules", "admin_only": True,
#   "actions": [
#       {"type": "send_message", "content": "...", "is_embed": bool, "embed_title": "...", "embed_color": int},
#       {"type": "give_role", "role_id": ...},
#       {"type": "remove_role", "role_id": ...},
#       {"type": "rename_channel", "new_name": "..."},
#       {"type": "delete_trigger"},
#       {"type": "add_reaction", "emoji": "..."},
#       {"type": "kick_member"},
#       {"type": "ban_member"},
#   ],
# }

VALID_PREFIXES = ["!", "?", ".", ";", "$", "%", "-"]


def load_custom_commands():
    if os.path.exists(CUSTOM_COMMANDS_FILE):
        with open(CUSTOM_COMMANDS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_custom_commands(data):
    with open(CUSTOM_COMMANDS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def make_command_key(prefix: str, name: str) -> str:
    return f"{prefix}{name.lower()}"


MAX_CUSTOM_COMMANDS = 50


def save_custom_command(prefix: str, name: str, actions: list, admin_only: bool = True) -> bool:
    """Retourne False si la limite de commandes est atteinte (et que ce n'est pas une mise à jour
    d'une commande existante), True sinon."""
    data = load_custom_commands()
    key = make_command_key(prefix, name)

    if key not in data and len(data) >= MAX_CUSTOM_COMMANDS:
        return False

    data[key] = {
        "prefix": prefix,
        "name": name.lower(),
        "admin_only": admin_only,
        "actions": actions,
    }
    save_custom_commands(data)
    return True


def get_custom_command(prefix: str, name: str):
    data = load_custom_commands()
    return data.get(make_command_key(prefix, name))


def delete_custom_command(prefix: str, name: str) -> bool:
    data = load_custom_commands()
    key = make_command_key(prefix, name)
    if key in data:
        del data[key]
        save_custom_commands(data)
        return True
    return False


def delete_custom_command_by_key(key: str) -> bool:
    data = load_custom_commands()
    if key in data:
        del data[key]
        save_custom_commands(data)
        return True
    return False


def list_custom_commands():
    return load_custom_commands()




async def resolve_target_member_strict(message: discord.Message) -> discord.Member | None:
    """Comme resolve_target_member, mais SANS fallback sur soi-même.
    À utiliser pour les actions destructives (kick/ban) : si personne n'est explicitement
    visé (reply ou mention), retourne None plutôt que de cibler l'auteur par accident."""
    if message.reference and message.reference.message_id:
        try:
            replied = message.reference.resolved
            if replied is None:
                replied = await message.channel.fetch_message(message.reference.message_id)
            if isinstance(replied.author, discord.Member):
                return replied.author
            return message.guild.get_member(replied.author.id) or replied.author
        except (discord.NotFound, discord.Forbidden, AttributeError):
            pass

    if message.mentions:
        return message.mentions[0]

    return None


async def execute_custom_action(action: dict, message: discord.Message):
    """Exécute une seule action d'une commande personnalisée."""
    action_type = action.get("type")

    try:
        if action_type == "send_message":
            if action.get("is_embed"):
                embed = discord.Embed(
                    title=action.get("embed_title") or None,
                    description=action.get("content") or None,
                    color=action.get("embed_color", EMBED_COLOR),
                )
                await message.channel.send(embed=embed)
            else:
                await message.channel.send(action.get("content", ""))

        elif action_type == "give_role":
            role = message.guild.get_role(action["role_id"])
            target = await resolve_target_member_strict(message)
            if target is None:
                await message.channel.send(
                    "⚠️ Please mention someone or reply to their message to use this command."
                )
            elif role:
                if role >= message.guild.me.top_role:
                    await message.channel.send(
                        f"⚠️ I can't assign **{role.name}** — it's positioned at or above my own highest role."
                    )
                else:
                    await target.add_roles(role, reason="Custom command")

        elif action_type == "remove_role":
            role = message.guild.get_role(action["role_id"])
            target = await resolve_target_member_strict(message)
            if target is None:
                await message.channel.send(
                    "⚠️ Please mention someone or reply to their message to use this command."
                )
            elif role:
                if role >= message.guild.me.top_role:
                    await message.channel.send(
                        f"⚠️ I can't remove **{role.name}** — it's positioned at or above my own highest role."
                    )
                else:
                    await target.remove_roles(role, reason="Custom command")

        elif action_type == "rename_channel":
            await message.channel.edit(name=action["new_name"])

        elif action_type == "delete_trigger":
            try:
                await message.delete()
            except (discord.NotFound, discord.Forbidden):
                pass

        elif action_type == "add_reaction":
            emoji = parse_emoji_input(action.get("emoji"))
            if emoji:
                await message.add_reaction(emoji)

        elif action_type == "kick_member":
            target = await resolve_target_member_strict(message)
            if target is None:
                await message.channel.send(
                    "⚠️ No target specified — reply to a message or mention someone to use this action."
                )
            elif target.id == message.author.id:
                await message.channel.send("⚠️ You can't kick yourself with this command.")
            elif target.top_role >= message.guild.me.top_role:
                await message.channel.send(f"⚠️ I can't kick **{target.display_name}** — their role is at or above mine.")
            else:
                await target.kick(reason="Custom command")

        elif action_type == "ban_member":
            target = await resolve_target_member_strict(message)
            if target is None:
                await message.channel.send(
                    "⚠️ No target specified — reply to a message or mention someone to use this action."
                )
            elif target.id == message.author.id:
                await message.channel.send("⚠️ You can't ban yourself with this command.")
            elif target.top_role >= message.guild.me.top_role:
                await message.channel.send(f"⚠️ I can't ban **{target.display_name}** — their role is at or above mine.")
            else:
                await target.ban(reason="Custom command")

    except discord.Forbidden:
        print(f"❌ Permissions insuffisantes pour l'action '{action_type}' dans #{message.channel.name}")
    except Exception as e:
        print(f"❌ Erreur lors de l'action '{action_type}' : {e}")


async def try_run_custom_command(message: discord.Message) -> bool:
    """Vérifie si le message déclenche une commande personnalisée, et l'exécute si oui.
    Retourne True si une commande a été exécutée (pour stopper le traitement on_message ensuite).
    Note sécurité : pas de risque de boucle infinie ici — même si une action 'send_message' contient
    un préfixe de commande, ce message est envoyé PAR le bot, et on_message ignore déjà
    message.author.bot avant même d'appeler cette fonction."""
    content = message.content.strip()
    if not content or len(content) < 2:
        return False

    prefix = content[0]
    if prefix not in VALID_PREFIXES:
        return False

    rest = content[1:].strip()
    if not rest:
        return False

    # Le nom de la commande est le premier "mot" après le préfixe
    name = rest.split()[0].lower()
    cmd = get_custom_command(prefix, name)
    if not cmd:
        return False

    if cmd.get("admin_only", True):
        if not message.author.guild_permissions.administrator:
            return False

    for action in cmd.get("actions", []):
        await execute_custom_action(action, message)

    # Supprime le message déclencheur pour garder le chat propre,
    # sauf si une action "delete_trigger" était déjà configurée (évite une double tentative)
    has_delete_action = any(a.get("type") == "delete_trigger" for a in cmd.get("actions", []))
    if not has_delete_action:
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    return True


class CustomCommandBuilderView(discord.ui.View):
    def __init__(self, author_id: int, existing: dict = None):
        super().__init__(timeout=600)
        self.author_id = author_id
        if existing:
            # Mode édition : on part d'une copie pour ne pas modifier l'original avant sauvegarde
            self.draft = {
                "prefix": existing["prefix"],
                "name": existing["name"],
                "admin_only": existing["admin_only"],
                "actions": [dict(a) for a in existing["actions"]],
            }
            self.original_key = make_command_key(existing["prefix"], existing["name"])
        else:
            self.draft = {
                "prefix": "!",
                "name": "",
                "admin_only": True,
                "actions": [],
            }
            self.original_key = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Only the person who started this can edit it.", ephemeral=True)
            return False
        return True

    def summary_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🛠️ Custom Command Builder", color=EMBED_COLOR)
        trigger = f"{self.draft['prefix']}{self.draft['name'] or '<name not set>'}"
        embed.add_field(name="Trigger", value=f"`{trigger}`", inline=True)
        embed.add_field(name="Admin only", value="Yes" if self.draft["admin_only"] else "No", inline=True)

        if not self.draft["actions"]:
            embed.add_field(name="Actions", value="*No actions added yet.*", inline=False)
        else:
            lines = []
            for i, action in enumerate(self.draft["actions"], start=1):
                lines.append(f"{i}. {describe_action(action)}")
            embed.add_field(name="Actions", value="\n".join(lines), inline=False)

        return embed

    async def refresh(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.summary_embed(), view=self)

    @discord.ui.button(label="Set Prefix", style=discord.ButtonStyle.secondary, row=0)
    async def btn_set_prefix(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=120)
        view.add_item(PrefixSelect(self))
        await interaction.response.send_message("Choose a prefix:", view=view, ephemeral=True)

    @discord.ui.button(label="Set Name", style=discord.ButtonStyle.secondary, row=0)
    async def btn_set_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CommandNameModal(self))

    @discord.ui.button(label="Toggle Admin Only", style=discord.ButtonStyle.secondary, row=0)
    async def btn_toggle_admin(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.draft["admin_only"] = not self.draft["admin_only"]
        await self.refresh(interaction)

    @discord.ui.button(label="➕ Add Action", style=discord.ButtonStyle.primary, row=1)
    async def btn_add_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = discord.ui.View(timeout=120)
        view.add_item(ActionTypeSelect(self))
        await interaction.response.send_message("What should this action do?", view=view, ephemeral=True)

    @discord.ui.button(label="➖ Remove Action", style=discord.ButtonStyle.secondary, row=1)
    async def btn_remove_action(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.draft["actions"]:
            await interaction.response.send_message("No actions to remove.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(RemoveActionSelect(self))
        await interaction.response.send_message("Select an action to remove:", view=view, ephemeral=True)

    @discord.ui.button(label="✅ Save Command", style=discord.ButtonStyle.success, row=2)
    async def btn_save(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.draft["name"]:
            await interaction.response.send_message("❌ You must set a name first.", ephemeral=True)
            return
        if not self.draft["actions"]:
            await interaction.response.send_message("❌ You must add at least one action first.", ephemeral=True)
            return

        new_key = make_command_key(self.draft["prefix"], self.draft["name"])

        # Si on édite une commande existante et que le préfixe/nom a changé,
        # on retire l'ancienne clé pour ne pas laisser de doublon.
        if self.original_key and self.original_key != new_key:
            delete_custom_command_by_key(self.original_key)

        saved = save_custom_command(
            self.draft["prefix"], self.draft["name"], self.draft["actions"], self.draft["admin_only"]
        )
        if not saved:
            await interaction.response.send_message(
                f"❌ Limit of {MAX_CUSTOM_COMMANDS} custom commands reached. Delete one with /commanddelete first.",
                ephemeral=True,
            )
            return

        trigger = f"{self.draft['prefix']}{self.draft['name']}"
        await interaction.response.edit_message(
            content=f"✅ Command `{trigger}` saved! It now works in every channel.",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger, row=2)
    async def btn_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="❌ Cancelled.", embed=None, view=None)


def describe_action(action: dict) -> str:
    t = action.get("type")
    if t == "send_message":
        kind = "embed" if action.get("is_embed") else "text"
        preview = (action.get("content") or "")[:40]
        return f"Send {kind}: \"{preview}...\"" if len(action.get("content", "")) > 40 else f"Send {kind}: \"{preview}\""
    elif t == "give_role":
        return f"Give role <@&{action['role_id']}>"
    elif t == "remove_role":
        return f"Remove role <@&{action['role_id']}>"
    elif t == "rename_channel":
        return f"Rename channel to \"{action['new_name']}\""
    elif t == "delete_trigger":
        return "Delete the triggering message"
    elif t == "add_reaction":
        return f"Add reaction {action.get('emoji')}"
    elif t == "kick_member":
        return "Kick mentioned member(s)"
    elif t == "ban_member":
        return "Ban mentioned member(s)"
    return "Unknown action"


class PrefixSelect(discord.ui.Select):
    def __init__(self, builder: CustomCommandBuilderView):
        self.builder = builder
        options = [discord.SelectOption(label=p, value=p) for p in VALID_PREFIXES]
        super().__init__(placeholder="Choose a prefix...", options=options)

    async def callback(self, interaction: discord.Interaction):
        self.builder.draft["prefix"] = self.values[0]
        await interaction.response.edit_message(content=f"✅ Prefix set to `{self.values[0]}`", view=None)


class CommandNameModal(discord.ui.Modal, title="Command Name"):
    name_input = discord.ui.TextInput(label="Name (no spaces)", max_length=30, required=True)

    def __init__(self, builder: CustomCommandBuilderView):
        super().__init__()
        self.builder = builder

    async def on_submit(self, interaction: discord.Interaction):
        clean_name = str(self.name_input.value).strip().split()[0].lower()
        self.builder.draft["name"] = clean_name
        await self.builder.refresh(interaction)


class ActionTypeSelect(discord.ui.Select):
    def __init__(self, builder: CustomCommandBuilderView):
        self.builder = builder
        options = [
            discord.SelectOption(label="Send a message", value="send_message", emoji="💬"),
            discord.SelectOption(label="Give a role", value="give_role", emoji="✅"),
            discord.SelectOption(label="Remove a role", value="remove_role", emoji="🚫"),
            discord.SelectOption(label="Rename this channel", value="rename_channel", emoji="✏️"),
            discord.SelectOption(label="Delete the trigger message", value="delete_trigger", emoji="🗑️"),
            discord.SelectOption(label="Add a reaction", value="add_reaction", emoji="😀"),
            discord.SelectOption(label="Kick mentioned member", value="kick_member", emoji="👋"),
            discord.SelectOption(label="Ban mentioned member", value="ban_member", emoji="🔨"),
        ]
        super().__init__(placeholder="Choose an action type...", options=options)

    async def callback(self, interaction: discord.Interaction):
        action_type = self.values[0]

        if action_type == "send_message":
            await interaction.response.send_modal(SendMessageActionModal(self.builder))
        elif action_type in ("give_role", "remove_role"):
            view = discord.ui.View(timeout=120)
            view.add_item(RoleActionSelect(self.builder, action_type))
            await interaction.response.send_message("Choose a role:", view=view, ephemeral=True)
        elif action_type == "rename_channel":
            await interaction.response.send_modal(RenameChannelActionModal(self.builder))
        elif action_type == "add_reaction":
            await interaction.response.send_modal(AddReactionActionModal(self.builder))
        else:
            # Actions sans configuration supplémentaire
            self.builder.draft["actions"].append({"type": action_type})
            await interaction.response.edit_message(content=f"✅ Action added!", view=None)


class SendMessageActionModal(discord.ui.Modal, title="Send Message Action"):
    content_input = discord.ui.TextInput(
        label="Message content", style=discord.TextStyle.paragraph, max_length=2000, required=True
    )
    embed_title_input = discord.ui.TextInput(
        label="Embed title (leave empty for plain text)", max_length=256, required=False
    )

    def __init__(self, builder: CustomCommandBuilderView):
        super().__init__()
        self.builder = builder

    async def on_submit(self, interaction: discord.Interaction):
        is_embed = bool(str(self.embed_title_input.value).strip())
        self.builder.draft["actions"].append({
            "type": "send_message",
            "content": str(self.content_input.value),
            "is_embed": is_embed,
            "embed_title": str(self.embed_title_input.value) if is_embed else "",
            "embed_color": EMBED_COLOR,
        })
        await self.builder.refresh(interaction)


class RoleActionSelect(discord.ui.RoleSelect):
    def __init__(self, builder: CustomCommandBuilderView, action_type: str):
        self.builder = builder
        self.action_type = action_type
        super().__init__(placeholder="Choose a role...")

    async def callback(self, interaction: discord.Interaction):
        role = self.values[0]
        self.builder.draft["actions"].append({"type": self.action_type, "role_id": role.id})
        await interaction.response.edit_message(content=f"✅ Action added: {self.action_type} {role.mention}", view=None)


class RenameChannelActionModal(discord.ui.Modal, title="Rename Channel Action"):
    name_input = discord.ui.TextInput(label="New channel name", max_length=100, required=True)

    def __init__(self, builder: CustomCommandBuilderView):
        super().__init__()
        self.builder = builder

    async def on_submit(self, interaction: discord.Interaction):
        self.builder.draft["actions"].append({"type": "rename_channel", "new_name": str(self.name_input.value)})
        await self.builder.refresh(interaction)


class AddReactionActionModal(discord.ui.Modal, title="Add Reaction Action"):
    emoji_input = discord.ui.TextInput(label="Emoji (unicode or :custom:)", max_length=50, required=True)

    def __init__(self, builder: CustomCommandBuilderView):
        super().__init__()
        self.builder = builder

    async def on_submit(self, interaction: discord.Interaction):
        self.builder.draft["actions"].append({"type": "add_reaction", "emoji": str(self.emoji_input.value)})
        await self.builder.refresh(interaction)


class RemoveActionSelect(discord.ui.Select):
    def __init__(self, builder: CustomCommandBuilderView):
        self.builder = builder
        options = [
            discord.SelectOption(label=f"{i+1}. {describe_action(a)}"[:100], value=str(i))
            for i, a in enumerate(builder.draft["actions"])
        ][:25]
        super().__init__(placeholder="Choose an action to remove...", options=options)

    async def callback(self, interaction: discord.Interaction):
        index = int(self.values[0])
        del self.builder.draft["actions"][index]
        await interaction.response.edit_message(content="✅ Action removed.", view=None)


# Note: /commandcreate, /commanddelete, /commandlist sont enregistrées plus bas,
# une fois que "tree" est défini (après la création du bot).




# ─────────────────────────────────────────────
#  GESTION DES MESSAGES STICKY (/stick, /unstick)
# ─────────────────────────────────────────────
# Structure stockée par channel_id (str) :
# {
#   "content": "..." | None,         (texte simple, si pas d'embed)
#   "embed": {...} | None,           (draft d'embed, si /stick sans texte)
#   "buttons": [...],                (boutons-liens, si embed)
#   "last_message_id": ...,          (dernier message sticky envoyé par le bot)
# }

def load_sticky():
    if os.path.exists(STICKY_FILE):
        with open(STICKY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_sticky(data):
    with open(STICKY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def set_sticky(channel_id: int, content: str = None, embed_draft: dict = None, last_message_id: int = None):
    data = load_sticky()
    data[str(channel_id)] = {
        "content": content,
        "embed": embed_draft,
        "last_message_id": last_message_id,
    }
    save_sticky(data)


def update_sticky_last_id(channel_id: int, message_id: int):
    data = load_sticky()
    if str(channel_id) in data:
        data[str(channel_id)]["last_message_id"] = message_id
        save_sticky(data)


def remove_sticky(channel_id: int) -> bool:
    data = load_sticky()
    if str(channel_id) in data:
        del data[str(channel_id)]
        save_sticky(data)
        return True
    return False


def get_sticky(channel_id: int):
    data = load_sticky()
    return data.get(str(channel_id))


# ─────────────────────────────────────────────
#  GESTION DES GIVEAWAYS
# ─────────────────────────────────────────────
# Structure stockée par message_id (str) :
# {
#   "channel_id": ..., "prize": ..., "winners_count": ...,
#   "end_timestamp": ... (unix), "participants": [user_id, ...],
#   "ended": bool, "host_id": ...,
# }

def load_giveaways():
    if os.path.exists(GIVEAWAYS_FILE):
        with open(GIVEAWAYS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_giveaways(data):
    with open(GIVEAWAYS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def create_giveaway(message_id: int, channel_id: int, prize: str, winners_count: int, end_timestamp: float, host_id: int):
    data = load_giveaways()
    data[str(message_id)] = {
        "channel_id": channel_id,
        "prize": prize,
        "winners_count": winners_count,
        "end_timestamp": end_timestamp,
        "participants": [],
        "ended": False,
        "host_id": host_id,
    }
    save_giveaways(data)


def get_giveaway(message_id: int):
    data = load_giveaways()
    return data.get(str(message_id))


def add_participant(message_id: int, user_id: int) -> bool:
    """Retourne True si ajouté, False si déjà inscrit."""
    data = load_giveaways()
    g = data.get(str(message_id))
    if not g:
        return False
    if user_id in g["participants"]:
        return False
    g["participants"].append(user_id)
    save_giveaways(data)
    return True


def mark_giveaway_ended(message_id: int):
    data = load_giveaways()
    if str(message_id) in data:
        data[str(message_id)]["ended"] = True
        save_giveaways(data)


def get_active_giveaways():
    data = load_giveaways()
    return {mid: g for mid, g in data.items() if not g.get("ended")}


# ─────────────────────────────────────────────
#  GESTION DES COMMANDES (orders)
# ─────────────────────────────────────────────
# Structure stockée par order_id (str, incrémental) :
# {
#   "buyer_id": ..., "buyer_contact": "...", "model": "...",
#   "payment_email": "..." | None, "status": "pending_payment" | "awaiting_review" | "accepted" | "rejected",
#   "staff_message_id": ... | None, "created_at": unix timestamp,
# }

def load_orders():
    if os.path.exists(ORDERS_FILE):
        with open(ORDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_orders(data):
    with open(ORDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def create_order(buyer_id: int, buyer_contact: str, model: str) -> str:
    data = load_orders()
    order_id = str(int(time.time() * 1000))  # ID unique basé sur le timestamp
    data[order_id] = {
        "buyer_id": buyer_id,
        "buyer_contact": buyer_contact,
        "model": model,
        "payment_email": None,
        "status": "pending_payment",
        "staff_message_id": None,
        "created_at": time.time(),
    }
    save_orders(data)
    return order_id


def update_order(order_id: str, **kwargs):
    data = load_orders()
    if order_id in data:
        data[order_id].update(kwargs)
        save_orders(data)


def get_order(order_id: str):
    data = load_orders()
    return data.get(order_id)


def load_bot_state():
    if os.path.exists(BOT_STATE_FILE):
        with open(BOT_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_bot_state(data):
    with open(BOT_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ─────────────────────────────────────────────
#  BOT
# ─────────────────────────────────────────────

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


def find_ticket_opener(channel: discord.TextChannel):
    for target, overwrite in channel.overwrites.items():
        if isinstance(target, discord.Member) and not target.bot:
            if overwrite.view_channel:
                return target
    return None


def is_ticket_channel(channel: discord.TextChannel) -> bool:
    name_match = any(channel.name.lower().startswith(p) for p in TICKET_CHANNEL_PREFIXES)
    if TICKET_CATEGORY_ID:
        return name_match and channel.category_id == TICKET_CATEGORY_ID
    return name_match


def detect_kind(channel: discord.TextChannel) -> str:
    """Détermine quel template utiliser selon le nom du salon."""
    name = channel.name.lower()
    if "support" in name:
        return "support"
    if "weight" in name:
        return "weights"
    return "normal"


def build_embed(channel: discord.TextChannel) -> discord.Embed:
    kind = detect_kind(channel)
    template = get_template(kind)

    description = f"{template['description']}\n\n{CREDIT_LINE}"

    embed = discord.Embed(
        title=template["title"],
        description=description,
        color=EMBED_COLOR,
    )

    if template.get("thumbnail_url"):
        embed.set_thumbnail(url=template["thumbnail_url"])
    if template.get("image_url"):
        embed.set_image(url=template["image_url"])

    return embed


@bot.event
async def on_ready():
    await tree.sync()
    bot.add_view(GiveawayView())
    bot.add_view(OrderStartView())
    bot.add_view(FirstClickView())
    if not hasattr(bot, "_giveaway_loop_started"):
        bot._giveaway_loop_started = True
        asyncio.create_task(giveaway_checker_loop())

    if not hasattr(bot, "_reminder_loop_started"):
        bot._reminder_loop_started = True
        asyncio.create_task(ticket_reminder_loop())

    if ORDER_PANEL_CHANNEL_ID:
        await ensure_order_panel_posted()

    print(f"✅ Bot connecté en tant que {bot.user} (ID: {bot.user.id})")
    print(f"   Surveillance des salons : {', '.join(TICKET_CHANNEL_PREFIXES)}")


async def ensure_order_panel_posted():
    """Poste le panneau de commande dans ORDER_PANEL_CHANNEL_ID s'il n'y est pas déjà."""
    channel = bot.get_channel(ORDER_PANEL_CHANNEL_ID)
    if not channel:
        print(f"❌ ORDER_PANEL_CHANNEL_ID {ORDER_PANEL_CHANNEL_ID} introuvable.")
        return

    state = load_bot_state()
    existing_id = state.get("order_panel_message_id")

    if existing_id:
        try:
            await channel.fetch_message(existing_id)
            return  # Le panneau existe déjà, rien à faire
        except (discord.NotFound, discord.Forbidden):
            pass  # Le message a été supprimé, on va en reposter un

    embed = discord.Embed(title=ORDER_EMBED_TITLE, description=ORDER_EMBED_DESCRIPTION, color=EMBED_COLOR)
    view = OrderStartView()
    message = await channel.send(embed=embed, view=view)

    state["order_panel_message_id"] = message.id
    save_bot_state(state)
    print(f"📌 Order panel posted in #{channel.name}")


@bot.event
async def on_guild_channel_delete(channel):
    if isinstance(channel, discord.TextChannel):
        remove_ticket_tracking(channel.id)


@bot.event
async def on_guild_channel_update(before, after):
    if not isinstance(before, discord.TextChannel) or not isinstance(after, discord.TextChannel):
        return
    if before.name == after.name:
        return

    old_name_was_ticket = is_ticket_channel(before)
    tracked_as_ticket = str(after.id) in load_reminders().get("tickets", {})
    renamed_to_done = after.name.strip().lower() == "done"

    if not renamed_to_done or not (old_name_was_ticket or tracked_as_ticket):
        return

    try:
        await after.send(DONE_REVIEW_MESSAGE)
        print(f"📨 Review request sent in #{after.name}")
    except discord.Forbidden:
        print(f"❌ Permissions insuffisantes pour envoyer la demande de review dans #{after.name}")
    except Exception as e:
        print(f"❌ Erreur demande de review dans #{after.name} : {e}")
    finally:
        remove_ticket_tracking(after.id)


@bot.event
async def on_guild_channel_create(channel):
    if not isinstance(channel, discord.TextChannel):
        return
    if not is_ticket_channel(channel):
        return

    await asyncio.sleep(3)

    try:
        embed = build_embed(channel)
        opener = find_ticket_opener(channel)
        if opener:
            await channel.send(content=opener.mention, embed=embed)
            register_new_ticket(channel.id, opener.id)
        else:
            await channel.send(embed=embed)
            register_new_ticket(channel.id, None)
        print(f"📨 Message envoyé dans #{channel.name}")
    except discord.Forbidden:
        print(f"❌ Permissions insuffisantes pour #{channel.name}")
    except Exception as e:
        print(f"❌ Erreur dans #{channel.name} : {e}")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not isinstance(message.channel, discord.TextChannel):
        return

    # Commandes personnalisées (/commandcreate) — fonctionnent dans tous les salons
    if await try_run_custom_command(message):
        return

    # Mots-clés liés à un salon précis : fonctionnent partout
    # Mots-clés liés à un scope ticket : seulement dans les salons tickets
    ticket_kind = detect_kind(message.channel) if is_ticket_channel(message.channel) else None
    matches = find_matching_keywords(message, ticket_kind)

    for entry in matches:
        try:
            if entry["response_type"] == "text":
                await message.channel.send(entry["text"])
            else:
                embed = discord.Embed(
                    title=entry.get("embed_title", ""),
                    description=entry.get("embed_description", ""),
                    color=EMBED_COLOR,
                )
                await message.channel.send(embed=embed)
        except Exception as e:
            print(f"❌ Erreur auto-réponse mot-clé '{entry['keyword']}' : {e}")

    # Gestion du sticky message : repost si quelqu'un a parlé après le dernier sticky
    sticky = get_sticky(message.channel.id)
    if sticky and message.id != sticky.get("last_message_id"):
        try:
            if sticky.get("last_message_id"):
                try:
                    old_msg = await message.channel.fetch_message(sticky["last_message_id"])
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

            send_view = None
            if sticky.get("buttons"):
                send_view = discord.ui.View(timeout=None)
                for btn_data in sticky["buttons"]:
                    link_btn = discord.ui.Button(
                        label=btn_data["label"],
                        url=btn_data["url"],
                        emoji=parse_emoji_input(btn_data.get("emoji")),
                        style=discord.ButtonStyle.link,
                    )
                    send_view.add_item(link_btn)

            if sticky.get("embed"):
                embed = render_draft(sticky["embed"])
                new_msg = await message.channel.send(embed=embed, view=send_view) if send_view else await message.channel.send(embed=embed)
            else:
                new_msg = await message.channel.send(content=sticky.get("content") or "")

            update_sticky_last_id(message.channel.id, new_msg.id)
        except Exception as e:
            print(f"❌ Erreur repost sticky dans #{message.channel.name} : {e}")

    # Suivi d'activité pour le système de relance des tickets inactifs
    if is_ticket_channel(message.channel):
        update_ticket_activity(message.channel.id, message.author.id)

    await bot.process_commands(message)


# ─────────────────────────────────────────────
#  SYSTÈME DE RELANCE DES TICKETS INACTIFS
# ─────────────────────────────────────────────

REMINDER_DELAY_SECONDS = 48 * 3600  # 48 heures
DEFAULT_REMINDER_TEXT = (
    "Hey! We noticed your ticket has been inactive for a while. "
    "Just checking in to see if you still need help — feel free to reply whenever you're ready!"
)


def load_reminders():
    if os.path.exists(REMINDERS_FILE):
        with open(REMINDERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"text": DEFAULT_REMINDER_TEXT, "tickets": {}}


def save_reminders(data):
    with open(REMINDERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_reminder_text() -> str:
    return load_reminders().get("text", DEFAULT_REMINDER_TEXT)


def set_reminder_text(text: str):
    data = load_reminders()
    data["text"] = text
    save_reminders(data)


def update_ticket_activity(channel_id: int, opener_id: int = None):
    """Met à jour le timestamp de dernière activité d'un ticket, et réinitialise son statut de relance."""
    data = load_reminders()
    key = str(channel_id)
    existing = data["tickets"].get(key, {})
    data["tickets"][key] = {
        "last_activity": time.time(),
        "opener_id": existing.get("opener_id") or opener_id,
        "reminded": False,
    }
    save_reminders(data)


def register_new_ticket(channel_id: int, opener_id: int):
    data = load_reminders()
    data["tickets"][str(channel_id)] = {
        "last_activity": time.time(),
        "opener_id": opener_id,
        "reminded": False,
    }
    save_reminders(data)


def mark_reminded(channel_id: int):
    data = load_reminders()
    key = str(channel_id)
    if key in data["tickets"]:
        data["tickets"][key]["reminded"] = True
        save_reminders(data)


def remove_ticket_tracking(channel_id: int):
    data = load_reminders()
    key = str(channel_id)
    if key in data["tickets"]:
        del data["tickets"][key]
        save_reminders(data)


async def ticket_reminder_loop():
    await bot.wait_until_ready()
    while True:
        try:
            data = load_reminders()
            now = time.time()
            reminder_text = data.get("text", DEFAULT_REMINDER_TEXT)

            for channel_id_str, info in list(data["tickets"].items()):
                if info.get("reminded"):
                    continue

                if now - info["last_activity"] < REMINDER_DELAY_SECONDS:
                    continue

                channel_id = int(channel_id_str)
                channel = bot.get_channel(channel_id)

                # Le salon n'existe plus (ticket fermé) : on arrête de le suivre
                if not channel:
                    remove_ticket_tracking(channel_id)
                    continue

                opener_id = info.get("opener_id")
                if not opener_id:
                    opener = find_ticket_opener(channel)
                    opener_id = opener.id if opener else None

                if opener_id:
                    user = bot.get_user(opener_id) or await bot.fetch_user(opener_id)
                    try:
                        await user.send(reminder_text)
                        print(f"📨 Reminder DM sent to {opener_id} for ticket #{channel.name}")
                    except discord.Forbidden:
                        print(f"❌ Could not DM {opener_id} (DMs closed) for ticket #{channel.name}")

                mark_reminded(channel_id)

        except Exception as e:
            print(f"❌ Erreur dans ticket_reminder_loop : {e}")

        await asyncio.sleep(1800)  # vérifie toutes les 30 minutes


class ReminderTextModal(discord.ui.Modal, title="Configure Inactivity Reminder"):
    text_input = discord.ui.TextInput(
        label="Reminder message (sent via DM)",
        style=discord.TextStyle.paragraph,
        max_length=2000,
        required=True,
    )

    def __init__(self):
        super().__init__()
        self.text_input.default = get_reminder_text()

    async def on_submit(self, interaction: discord.Interaction):
        set_reminder_text(str(self.text_input.value))
        await interaction.response.send_message("✅ Reminder message updated!", ephemeral=True)


@tree.command(name="setupreminder", description="Configurer le message de relance envoyé après 48h d'inactivité")
@app_commands.checks.has_permissions(administrator=True)
async def setupreminder_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(ReminderTextModal())


# ─────────────────────────────────────────────
#  MODAL DE CONFIGURATION (/setup) — étape 1 : texte
# ─────────────────────────────────────────────

class SetupModal(discord.ui.Modal):
    def __init__(self, kind: str):
        super().__init__(title=f"Configure: {kind.capitalize()} Ticket Message")
        self.kind = kind

        current = get_template(kind)

        self.title_input = discord.ui.TextInput(
            label="Title",
            default=current["title"],
            max_length=256,
            required=True,
        )
        self.description_input = discord.ui.TextInput(
            label="Description",
            style=discord.TextStyle.paragraph,
            default=current["description"],
            max_length=4000,
            required=True,
        )
        self.thumbnail_input = discord.ui.TextInput(
            label="Thumbnail Image URL (small, top-right corner)",
            default=current.get("thumbnail_url", ""),
            required=False,
            placeholder="https://...",
        )
        self.image_input = discord.ui.TextInput(
            label="Main Image URL (large, bottom of message)",
            default=current.get("image_url", ""),
            required=False,
            placeholder="https://...",
        )

        self.add_item(self.title_input)
        self.add_item(self.description_input)
        self.add_item(self.thumbnail_input)
        self.add_item(self.image_input)

    async def on_submit(self, interaction: discord.Interaction):
        thumb = str(self.thumbnail_input.value).strip()
        img = str(self.image_input.value).strip()

        if not is_valid_url(thumb) or not is_valid_url(img):
            await interaction.response.send_message(
                "❌ Image URLs must start with `http://` or `https://`. Please try again.",
                ephemeral=True,
            )
            return

        set_template(
            self.kind,
            str(self.title_input.value),
            str(self.description_input.value),
            thumb,
            img,
        )

        preview = discord.Embed(
            title=str(self.title_input.value),
            description=f"{str(self.description_input.value)}\n\n{CREDIT_LINE}",
            color=EMBED_COLOR,
        )
        if thumb:
            preview.set_thumbnail(url=thumb)
        if img:
            preview.set_image(url=img)

        await interaction.response.send_message(
            content=f"✅ **{self.kind.capitalize()}** template updated! Here's a preview:",
            embed=preview,
            ephemeral=True,
        )


# ─────────────────────────────────────────────
#  COMMANDES SLASH
# ─────────────────────────────────────────────

kind_choices = [
    app_commands.Choice(name="Normal Ticket", value="normal"),
    app_commands.Choice(name="Weights Ticket", value="weights"),
    app_commands.Choice(name="Support Ticket", value="support"),
]


@tree.command(name="setup", description="Configurer le message automatique d'un type de ticket")
@app_commands.choices(type=kind_choices)
@app_commands.checks.has_permissions(administrator=True)
async def setup(interaction: discord.Interaction, type: app_commands.Choice[str]):
    await interaction.response.send_modal(SetupModal(type.value))


@tree.command(name="preview", description="Voir le message actuel d'un type de ticket")
@app_commands.choices(type=kind_choices)
async def preview(interaction: discord.Interaction, type: app_commands.Choice[str]):
    template = get_template(type.value)
    embed = discord.Embed(
        title=template["title"],
        description=f"{template['description']}\n\n{CREDIT_LINE}",
        color=EMBED_COLOR,
    )
    if template.get("thumbnail_url"):
        embed.set_thumbnail(url=template["thumbnail_url"])
    if template.get("image_url"):
        embed.set_image(url=template["image_url"])
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
#  MODAL POUR LE CONTENU D'UNE AUTO-RÉPONSE
# ─────────────────────────────────────────────

class KeywordTextModal(discord.ui.Modal):
    def __init__(self, keyword: str, scope: str = None, channel_id: int = None):
        super().__init__(title=f"Auto-reply for: {keyword}")
        self.keyword = keyword
        self.scope = scope
        self.channel_id = channel_id

        self.text_input = discord.ui.TextInput(
            label="Response text",
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=True,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        entry = {
            "keyword": self.keyword,
            "response_type": "text",
            "text": str(self.text_input.value),
            "scope": self.scope,
            "channel_id": self.channel_id,
        }
        add_keyword(entry)
        location = f"<#{self.channel_id}>" if self.channel_id else f"scope **{self.scope}**"
        await interaction.response.send_message(
            f"✅ Auto-reply added for keyword **{self.keyword}** in {location}.",
            ephemeral=True,
        )


class KeywordEmbedModal(discord.ui.Modal):
    def __init__(self, keyword: str, scope: str = None, channel_id: int = None):
        super().__init__(title=f"Auto-reply for: {keyword}")
        self.keyword = keyword
        self.scope = scope
        self.channel_id = channel_id

        self.embed_title_input = discord.ui.TextInput(
            label="Embed title",
            max_length=256,
            required=True,
        )
        self.embed_description_input = discord.ui.TextInput(
            label="Embed description",
            style=discord.TextStyle.paragraph,
            max_length=2000,
            required=True,
        )
        self.add_item(self.embed_title_input)
        self.add_item(self.embed_description_input)

    async def on_submit(self, interaction: discord.Interaction):
        entry = {
            "keyword": self.keyword,
            "response_type": "embed",
            "embed_title": str(self.embed_title_input.value),
            "embed_description": str(self.embed_description_input.value),
            "scope": self.scope,
            "channel_id": self.channel_id,
        }
        add_keyword(entry)
        location = f"<#{self.channel_id}>" if self.channel_id else f"scope **{self.scope}**"
        await interaction.response.send_message(
            f"✅ Auto-reply (embed) added for keyword **{self.keyword}** in {location}.",
            ephemeral=True,
        )


# ─────────────────────────────────────────────
#  COMMANDES MOTS-CLÉS
# ─────────────────────────────────────────────

scope_choices = [
    app_commands.Choice(name="All ticket types", value="all"),
    app_commands.Choice(name="Normal Ticket only", value="normal"),
    app_commands.Choice(name="Weights Ticket only", value="weights"),
    app_commands.Choice(name="Support Ticket only", value="support"),
]

response_type_choices = [
    app_commands.Choice(name="Simple text", value="text"),
    app_commands.Choice(name="Embed", value="embed"),
]


@tree.command(name="addkeyword", description="Ajouter une auto-réponse déclenchée par un mot-clé")
@app_commands.choices(scope=scope_choices, response_type=response_type_choices)
@app_commands.describe(
    keyword="Le mot-clé qui déclenche la réponse",
    scope="Type de ticket concerné (ignoré si tu choisis un salon précis)",
    response_type="Texte simple ou embed",
    channel="(Optionnel) Limiter à un salon précis, n'importe quel salon du serveur",
)
@app_commands.checks.has_permissions(administrator=True)
async def addkeyword(
    interaction: discord.Interaction,
    keyword: str,
    response_type: app_commands.Choice[str],
    scope: app_commands.Choice[str] = None,
    channel: discord.TextChannel = None,
):
    if not channel and not scope:
        await interaction.response.send_message(
            "❌ You must choose either a `scope` (ticket type) or a specific `channel`.",
            ephemeral=True,
        )
        return

    scope_value = scope.value if scope else None
    channel_id = channel.id if channel else None

    if response_type.value == "text":
        await interaction.response.send_modal(KeywordTextModal(keyword, scope_value, channel_id))
    else:
        await interaction.response.send_modal(KeywordEmbedModal(keyword, scope_value, channel_id))


@tree.command(name="listkeywords", description="Voir toutes les auto-réponses configurées")
async def listkeywords(interaction: discord.Interaction):
    data = load_keywords()
    if not data:
        await interaction.response.send_message("No keywords configured yet.", ephemeral=True)
        return

    embed = discord.Embed(title="🔑 Configured Keywords", color=EMBED_COLOR)
    for entry in data:
        if entry.get("channel_id"):
            location_label = f"<#{entry['channel_id']}>"
        else:
            location_label = entry.get("scope", "all").capitalize()
        type_label = "Embed" if entry["response_type"] == "embed" else "Text"
        embed.add_field(
            name=f"\"{entry['keyword']}\" — {location_label}",
            value=f"Type: {type_label}",
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="removekeyword", description="Retirer une auto-réponse")
@app_commands.checks.has_permissions(administrator=True)
async def removekeyword(interaction: discord.Interaction, keyword: str):
    success = remove_keyword(keyword)
    if success:
        await interaction.response.send_message(f"🚫 Keyword **{keyword}** removed.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ Keyword **{keyword}** not found.", ephemeral=True)


@tree.command(name="rename", description="Renommer le salon ticket actuel")
@app_commands.checks.has_permissions(administrator=True)
async def rename(interaction: discord.Interaction, name: str):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("❌ This command can only be used in a text channel.", ephemeral=True)
        return

    # Nettoyage basique du nom (Discord n'accepte pas espaces/majuscules dans les noms de salon)
    clean_name = name.lower().strip().replace(" ", "-")

    try:
        old_name = interaction.channel.name
        await interaction.channel.edit(name=clean_name)
        await interaction.response.send_message(f"✅ Channel renamed from **{old_name}** to **{clean_name}**.")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to rename this channel.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)


@tree.command(name="commandcreate", description="Créer une commande personnalisée (préfixe + actions)")
@app_commands.checks.has_permissions(administrator=True)
async def commandcreate_cmd(interaction: discord.Interaction):
    view = CustomCommandBuilderView(interaction.user.id)
    await interaction.response.send_message(embed=view.summary_embed(), view=view, ephemeral=True)


@tree.command(name="commanddelete", description="Supprimer une commande personnalisée")
@app_commands.describe(prefix="Le préfixe de la commande", name="Le nom de la commande")
@app_commands.checks.has_permissions(administrator=True)
async def commanddelete_cmd(interaction: discord.Interaction, prefix: str, name: str):
    if delete_custom_command(prefix, name):
        await interaction.response.send_message(f"🚫 Command `{prefix}{name}` deleted.", ephemeral=True)
    else:
        await interaction.response.send_message(f"⚠️ Command `{prefix}{name}` not found.", ephemeral=True)


@tree.command(name="commandlist", description="Voir toutes les commandes personnalisées")
@app_commands.checks.has_permissions(administrator=True)
async def commandlist_cmd(interaction: discord.Interaction):
    commands_data = list_custom_commands()
    if not commands_data:
        await interaction.response.send_message("No custom commands configured yet.", ephemeral=True)
        return

    embed = discord.Embed(title="🛠️ Custom Commands", color=EMBED_COLOR)
    for key, cmd in commands_data.items():
        trigger = f"{cmd['prefix']}{cmd['name']}"
        actions_desc = ", ".join(describe_action(a) for a in cmd["actions"])
        embed.add_field(name=trigger, value=actions_desc[:200] or "*No actions*", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


class ManageCommandSelect(discord.ui.Select):
    def __init__(self, author_id: int):
        self.author_id = author_id
        commands_data = list_custom_commands()
        options = [
            discord.SelectOption(
                label=f"{cmd['prefix']}{cmd['name']}",
                value=key,
                description=", ".join(describe_action(a) for a in cmd["actions"])[:100],
            )
            for key, cmd in commands_data.items()
        ][:25]
        super().__init__(placeholder="Choose a command to edit...", options=options)

    async def callback(self, interaction: discord.Interaction):
        commands_data = list_custom_commands()
        existing = commands_data.get(self.values[0])
        if not existing:
            await interaction.response.send_message("⚠️ This command no longer exists.", ephemeral=True)
            return

        view = CustomCommandBuilderView(self.author_id, existing=existing)
        await interaction.response.send_message(embed=view.summary_embed(), view=view, ephemeral=True)


@tree.command(name="managecommands", description="Modifier une commande personnalisée existante")
@app_commands.checks.has_permissions(administrator=True)
async def managecommands_cmd(interaction: discord.Interaction):
    commands_data = list_custom_commands()
    if not commands_data:
        await interaction.response.send_message("No custom commands configured yet.", ephemeral=True)
        return

    view = discord.ui.View(timeout=180)
    view.add_item(ManageCommandSelect(interaction.user.id))
    await interaction.response.send_message("Which command do you want to edit?", view=view, ephemeral=True)


# ─────────────────────────────────────────────
#  ÉDITEUR D'EMBED INTERACTIF (/embed)
# ─────────────────────────────────────────────

# État temporaire de l'embed en cours de construction, par message de panel
embed_drafts = {}

def draft_from_embed(embed: discord.Embed) -> dict:
    """Reconstruit un draft à partir d'un embed Discord existant (pour /editembed)."""
    draft = new_draft()
    draft["title"] = embed.title or ""
    draft["description"] = embed.description or ""
    draft["color"] = embed.color.value if embed.color else EMBED_COLOR
    draft["thumbnail_url"] = embed.thumbnail.url if embed.thumbnail else ""
    draft["image_url"] = embed.image.url if embed.image else ""
    draft["footer"] = embed.footer.text if embed.footer else ""
    draft["author_name"] = embed.author.name if embed.author else ""
    draft["author_icon_url"] = embed.author.icon_url if embed.author and embed.author.icon_url else ""
    draft["fields"] = [
        {"name": f.name, "value": f.value, "inline": f.inline}
        for f in embed.fields
    ]
    return draft


def draft_from_message(message: discord.Message) -> dict:
    """Reconstruit un draft complet (embed + boutons-liens) à partir d'un message existant."""
    if not message.embeds:
        return new_draft()
    draft = draft_from_embed(message.embeds[0])

    buttons = []
    for component in message.components:
        for child in component.children:
            if isinstance(child, discord.Button) and child.url:
                buttons.append({
                    "label": child.label or "",
                    "url": child.url,
                    "emoji": str(child.emoji) if child.emoji else None,
                })
    draft["buttons"] = buttons[:5]
    return draft


def new_draft():
    return {
        "title": "",
        "description": "",
        "color": EMBED_COLOR,
        "thumbnail_url": "",
        "image_url": "",
        "footer": "",
        "author_name": "",
        "author_icon_url": "",
        "fields": [],  # list of {"name": ..., "value": ..., "inline": bool}
        "buttons": [],  # list of {"label": ..., "url": ..., "emoji": ...}
    }


def render_draft(draft: dict) -> discord.Embed:
    embed = discord.Embed(
        title=draft["title"] or None,
        description=draft["description"] or None,
        color=draft["color"],
    )
    if draft["thumbnail_url"]:
        embed.set_thumbnail(url=draft["thumbnail_url"])
    if draft["image_url"]:
        embed.set_image(url=draft["image_url"])
    if draft["footer"]:
        embed.set_footer(text=draft["footer"])
    if draft["author_name"]:
        embed.set_author(name=draft["author_name"], icon_url=draft["author_icon_url"] or None)
    for f in draft["fields"]:
        embed.add_field(name=f["name"], value=f["value"], inline=f["inline"])
    if not draft["title"] and not draft["description"] and not draft["fields"]:
        embed.description = "*Your embed is empty. Use the buttons below to start building it.*"
    return embed


class MainContentModal(discord.ui.Modal, title="Title & Description"):
    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self.view_ref = view
        d = view.draft
        self.title_input = discord.ui.TextInput(label="Title", default=d["title"], required=False, max_length=256)
        self.desc_input = discord.ui.TextInput(label="Description", style=discord.TextStyle.paragraph, default=d["description"], required=False, max_length=4000)
        self.add_item(self.title_input)
        self.add_item(self.desc_input)

    async def on_submit(self, interaction: discord.Interaction):
        self.view_ref.draft["title"] = str(self.title_input.value)
        self.view_ref.draft["description"] = str(self.desc_input.value)
        await self.view_ref.refresh(interaction)


class ColorModal(discord.ui.Modal, title="Embed Color"):
    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self.view_ref = view
        self.color_input = discord.ui.TextInput(
            label="Hex color (e.g. FF5733)",
            default=f"{view.draft['color']:06X}",
            required=True,
            max_length=6,
            min_length=6,
        )
        self.add_item(self.color_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            value = int(str(self.color_input.value).strip().lstrip("#"), 16)
            self.view_ref.draft["color"] = value
            await self.view_ref.refresh(interaction)
        except ValueError:
            await interaction.response.send_message("❌ Invalid hex color. Example: `FF5733`", ephemeral=True)


class ImagesModal(discord.ui.Modal, title="Images"):
    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self.view_ref = view
        d = view.draft
        self.thumb_input = discord.ui.TextInput(label="Thumbnail URL (small)", default=d["thumbnail_url"], required=False, placeholder="https://...")
        self.image_input = discord.ui.TextInput(label="Main image URL (large)", default=d["image_url"], required=False, placeholder="https://...")
        self.add_item(self.thumb_input)
        self.add_item(self.image_input)

    async def on_submit(self, interaction: discord.Interaction):
        thumb = str(self.thumb_input.value).strip()
        img = str(self.image_input.value).strip()
        if not is_valid_url(thumb) or not is_valid_url(img):
            await interaction.response.send_message("❌ URLs must start with http:// or https://", ephemeral=True)
            return
        self.view_ref.draft["thumbnail_url"] = thumb
        self.view_ref.draft["image_url"] = img
        await self.view_ref.refresh(interaction)


class FooterAuthorModal(discord.ui.Modal, title="Footer & Author"):
    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self.view_ref = view
        d = view.draft
        self.footer_input = discord.ui.TextInput(label="Footer text", default=d["footer"], required=False, max_length=2048)
        self.author_input = discord.ui.TextInput(label="Author name", default=d["author_name"], required=False, max_length=256)
        self.author_icon_input = discord.ui.TextInput(label="Author icon URL", default=d["author_icon_url"], required=False, placeholder="https://...")
        self.add_item(self.footer_input)
        self.add_item(self.author_input)
        self.add_item(self.author_icon_input)

    async def on_submit(self, interaction: discord.Interaction):
        icon = str(self.author_icon_input.value).strip()
        if not is_valid_url(icon):
            await interaction.response.send_message("❌ Author icon URL must start with http:// or https://", ephemeral=True)
            return
        self.view_ref.draft["footer"] = str(self.footer_input.value)
        self.view_ref.draft["author_name"] = str(self.author_input.value)
        self.view_ref.draft["author_icon_url"] = icon
        await self.view_ref.refresh(interaction)


class AddFieldModal(discord.ui.Modal, title="Add Field"):
    name_input = discord.ui.TextInput(label="Field name", max_length=256, required=True)
    value_input = discord.ui.TextInput(label="Field value", style=discord.TextStyle.paragraph, max_length=1024, required=True)
    inline_input = discord.ui.TextInput(label="Inline? (yes/no)", default="no", max_length=3, required=True)

    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        if len(self.view_ref.draft["fields"]) >= 25:
            await interaction.response.send_message("❌ Maximum 25 fields reached.", ephemeral=True)
            return
        self.view_ref.draft["fields"].append({
            "name": str(self.name_input.value),
            "value": str(self.value_input.value),
            "inline": str(self.inline_input.value).strip().lower() in ("yes", "y", "true"),
        })
        await self.view_ref.refresh(interaction)


class RemoveFieldSelect(discord.ui.Select):
    def __init__(self, view: "EmbedBuilderView"):
        self.view_ref = view
        options = [
            discord.SelectOption(label=f["name"][:100], value=str(i))
            for i, f in enumerate(view.draft["fields"])
        ][:25]
        super().__init__(placeholder="Select a field to remove...", options=options)

    async def callback(self, interaction: discord.Interaction):
        index = int(self.values[0])
        del self.view_ref.draft["fields"][index]
        await self.view_ref.refresh(interaction, rebuild=True)


POPULAR_EMOJIS = ["🔗", "🌐", "✅", "📥", "💬", "🛒", "⭐", "🎮", "📄", "💳", "🚀", "📌", "🔒", "🎯", "📢"]


class EmojiPickSelect(discord.ui.Select):
    def __init__(self, view: "EmbedBuilderView", label: str, url: str, guild: discord.Guild):
        self.view_ref = view
        self.pending_label = label
        self.pending_url = url

        options = [discord.SelectOption(label="No emoji", value="__none__", emoji=None)]
        options += [discord.SelectOption(label=f"Emoji {e}", value=e, emoji=e) for e in POPULAR_EMOJIS]

        # Ajoute jusqu'à 9 emojis custom du serveur (pour rester sous la limite de 25 options)
        if guild:
            for custom in list(guild.emojis)[:9]:
                options.append(discord.SelectOption(label=f":{custom.name}:", value=str(custom), emoji=custom))

        super().__init__(placeholder="Choose an emoji for this button (optional)...", options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        chosen = self.values[0]
        emoji_value = None if chosen == "__none__" else chosen

        if len(self.view_ref.draft["buttons"]) >= 5:
            await interaction.response.send_message("❌ Maximum 5 link buttons reached.", ephemeral=True)
            return

        self.view_ref.draft["buttons"].append({
            "label": self.pending_label,
            "url": self.pending_url,
            "emoji": emoji_value,
        })
        self.view_ref.rebuild_link_buttons()
        await interaction.response.edit_message(
            content="✅ Button added!", embed=None, view=None
        )


class AddButtonModal(discord.ui.Modal, title="Add Link Button"):
    label_input = discord.ui.TextInput(label="Button label", max_length=80, required=True, placeholder="Rustdesk")
    url_input = discord.ui.TextInput(label="URL", max_length=512, required=True, placeholder="https://...")

    def __init__(self, view: "EmbedBuilderView"):
        super().__init__()
        self.view_ref = view

    async def on_submit(self, interaction: discord.Interaction):
        url = str(self.url_input.value).strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            await interaction.response.send_message("❌ URL must start with http:// or https://", ephemeral=True)
            return
        if len(self.view_ref.draft["buttons"]) >= 5:
            await interaction.response.send_message("❌ Maximum 5 link buttons reached.", ephemeral=True)
            return

        emoji_view = discord.ui.View(timeout=120)
        emoji_view.add_item(EmojiPickSelect(self.view_ref, str(self.label_input.value), url, interaction.guild))
        await interaction.response.send_message(
            "Now pick an emoji for this button:", view=emoji_view, ephemeral=True
        )


class RemoveButtonSelect(discord.ui.Select):
    def __init__(self, view: "EmbedBuilderView"):
        self.view_ref = view
        options = [
            discord.SelectOption(label=b["label"][:100], value=str(i))
            for i, b in enumerate(view.draft["buttons"])
        ][:25]
        super().__init__(placeholder="Select a button to remove...", options=options)

    async def callback(self, interaction: discord.Interaction):
        index = int(self.values[0])
        del self.view_ref.draft["buttons"][index]
        await self.view_ref.refresh(interaction, rebuild=True)


class EmbedBuilderView(discord.ui.View):
    def __init__(self, author_id: int, initial_draft: dict = None, target_message: discord.Message = None, stick_mode: bool = False):
        super().__init__(timeout=600)
        self.author_id = author_id
        self.draft = initial_draft if initial_draft is not None else new_draft()
        self.target_message = target_message
        self.stick_mode = stick_mode
        self.rebuild_link_buttons()
        if self.target_message:
            self.btn_send.label = "📤 Update Message"
        elif self.stick_mode:
            self.btn_send.label = "📌 Stick This"

    def rebuild_link_buttons(self):
        # Retire les anciens boutons-liens (row 3) avant d'en remettre des nouveaux
        for item in list(self.children):
            if isinstance(item, discord.ui.Button) and getattr(item, "_is_link_button", False):
                self.remove_item(item)

        for btn_data in self.draft["buttons"]:
            link_btn = discord.ui.Button(
                label=btn_data["label"],
                url=btn_data["url"],
                emoji=parse_emoji_input(btn_data["emoji"]),
                style=discord.ButtonStyle.link,
                row=3,
            )
            link_btn._is_link_button = True
            self.add_item(link_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Only the person who started this can edit it.", ephemeral=True)
            return False
        return True

    async def refresh(self, interaction: discord.Interaction, rebuild: bool = False):
        if rebuild:
            self.rebuild_link_buttons()
        embed = render_draft(self.draft)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Title & Description", style=discord.ButtonStyle.primary, row=0)
    async def btn_main(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MainContentModal(self))

    @discord.ui.button(label="Color", style=discord.ButtonStyle.primary, row=0)
    async def btn_color(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ColorModal(self))

    @discord.ui.button(label="Images", style=discord.ButtonStyle.primary, row=0)
    async def btn_images(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ImagesModal(self))

    @discord.ui.button(label="Footer & Author", style=discord.ButtonStyle.primary, row=0)
    async def btn_footer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(FooterAuthorModal(self))

    @discord.ui.button(label="➕ Add Field", style=discord.ButtonStyle.secondary, row=1)
    async def btn_add_field(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddFieldModal(self))

    @discord.ui.button(label="➖ Remove Field", style=discord.ButtonStyle.secondary, row=1)
    async def btn_remove_field(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.draft["fields"]:
            await interaction.response.send_message("No fields to remove.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(RemoveFieldSelect(self))
        await interaction.response.send_message("Select a field to remove:", view=view, ephemeral=True)

    @discord.ui.button(label="🔗 Add Link Button", style=discord.ButtonStyle.secondary, row=1)
    async def btn_add_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.draft["buttons"]) >= 5:
            await interaction.response.send_message("❌ Maximum 5 link buttons reached.", ephemeral=True)
            return
        await interaction.response.send_modal(AddButtonModal(self))

    @discord.ui.button(label="🔗 Remove Link Button", style=discord.ButtonStyle.secondary, row=1)
    async def btn_remove_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.draft["buttons"]:
            await interaction.response.send_message("No link buttons to remove.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(RemoveButtonSelect(self))
        await interaction.response.send_message("Select a link button to remove:", view=view, ephemeral=True)

    @discord.ui.button(label="🔄 Reset", style=discord.ButtonStyle.danger, row=2)
    async def btn_reset(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.draft = new_draft()
        await self.refresh(interaction, rebuild=True)

    @discord.ui.button(label="📤 Send Here", style=discord.ButtonStyle.success, row=2)
    async def btn_send(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = render_draft(self.draft)
        send_view = discord.ui.View(timeout=None)
        for btn_data in self.draft["buttons"]:
            link_btn = discord.ui.Button(
                label=btn_data["label"],
                url=btn_data["url"],
                emoji=parse_emoji_input(btn_data["emoji"]),
                style=discord.ButtonStyle.link,
            )
            send_view.add_item(link_btn)

        if self.target_message:
            # Mode édition : on met à jour le message existant au lieu d'en créer un nouveau
            try:
                if self.draft["buttons"]:
                    await self.target_message.edit(embed=embed, view=send_view)
                else:
                    await self.target_message.edit(embed=embed, view=None)
                await interaction.response.send_message("✅ Message updated!", ephemeral=True)
            except discord.Forbidden:
                await interaction.response.send_message("❌ I don't have permission to edit that message.", ephemeral=True)
            except Exception as e:
                await interaction.response.send_message(f"❌ Error updating message: {str(e)}", ephemeral=True)
            return

        if self.stick_mode:
            existing = get_sticky(interaction.channel.id)
            if existing and existing.get("last_message_id"):
                try:
                    old_msg = await interaction.channel.fetch_message(existing["last_message_id"])
                    await old_msg.delete()
                except (discord.NotFound, discord.Forbidden):
                    pass

            if self.draft["buttons"]:
                sent = await interaction.channel.send(embed=embed, view=send_view)
            else:
                sent = await interaction.channel.send(embed=embed)
            set_sticky(interaction.channel.id, content=None, embed_draft=self.draft, last_message_id=sent.id)
            data = load_sticky()
            data[str(interaction.channel.id)]["buttons"] = self.draft["buttons"]
            save_sticky(data)
            await interaction.response.send_message("📌 This embed is now stickied in this channel!", ephemeral=True)
            return

        if self.draft["buttons"]:
            await interaction.channel.send(embed=embed, view=send_view)
        else:
            await interaction.channel.send(embed=embed)
        await interaction.response.send_message("✅ Embed sent!", ephemeral=True)

    @discord.ui.button(label="💾 Save as Template", style=discord.ButtonStyle.success, row=2)
    async def btn_save_template(self, interaction: discord.Interaction, button: discord.ui.Button):
        async def save_callback(inner_interaction: discord.Interaction, kind: str):
            set_template(
                kind,
                self.draft["title"] or " ",
                self.draft["description"] or " ",
                self.draft["thumbnail_url"],
                self.draft["image_url"],
            )
            await inner_interaction.response.send_message(
                f"✅ Saved as **{kind.capitalize()}** template! (Note: footer/author/fields are not used in ticket templates.)",
                ephemeral=True,
            )

        class TemplateChoiceView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=60)

            @discord.ui.button(label="Normal", style=discord.ButtonStyle.secondary)
            async def normal_btn(self, i: discord.Interaction, b: discord.ui.Button):
                await save_callback(i, "normal")

            @discord.ui.button(label="Weights", style=discord.ButtonStyle.secondary)
            async def weights_btn(self, i: discord.Interaction, b: discord.ui.Button):
                await save_callback(i, "weights")

            @discord.ui.button(label="Support", style=discord.ButtonStyle.secondary)
            async def support_btn(self, i: discord.Interaction, b: discord.ui.Button):
                await save_callback(i, "support")

        await interaction.response.send_message(
            "Which template should this overwrite?", view=TemplateChoiceView(), ephemeral=True
        )


@tree.command(name="embed", description="Ouvrir l'éditeur d'embed interactif")
@app_commands.checks.has_permissions(administrator=True)
async def embed_cmd(interaction: discord.Interaction):
    view = EmbedBuilderView(interaction.user.id)
    embed = render_draft(view.draft)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ─────────────────────────────────────────────
#  COMMANDE /message — texte simple + boutons-liens (pas un embed)
# ─────────────────────────────────────────────

class MessageTextModal(discord.ui.Modal, title="Write Your Message"):
    text_input = discord.ui.TextInput(
        label="Message text",
        style=discord.TextStyle.paragraph,
        max_length=2000,
        required=True,
    )

    def __init__(self, view: "SimpleMessageView"):
        super().__init__()
        self.view_ref = view
        self.text_input.default = view.draft["content"]

    async def on_submit(self, interaction: discord.Interaction):
        self.view_ref.draft["content"] = str(self.text_input.value)
        await self.view_ref.refresh(interaction)


class SimpleMessageView(discord.ui.View):
    def __init__(self, author_id: int):
        super().__init__(timeout=600)
        self.author_id = author_id
        # On réutilise la même clé "buttons" que EmbedBuilderView pour que
        # AddButtonModal / EmojiPickSelect / RemoveButtonSelect fonctionnent sans modification.
        self.draft = {"content": "", "buttons": []}
        self.rebuild_link_buttons()

    def rebuild_link_buttons(self):
        for item in list(self.children):
            if isinstance(item, discord.ui.Button) and getattr(item, "_is_link_button", False):
                self.remove_item(item)

        for btn_data in self.draft["buttons"]:
            link_btn = discord.ui.Button(
                label=btn_data["label"],
                url=btn_data["url"],
                emoji=parse_emoji_input(btn_data["emoji"]),
                style=discord.ButtonStyle.link,
                row=2,
            )
            link_btn._is_link_button = True
            self.add_item(link_btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Only the person who started this can edit it.", ephemeral=True)
            return False
        return True

    def preview_content(self) -> str:
        return self.draft["content"] or "*Your message is empty. Click \"Write Message\" to start.*"

    async def refresh(self, interaction: discord.Interaction, rebuild: bool = False):
        if rebuild:
            self.rebuild_link_buttons()
        await interaction.response.edit_message(content=self.preview_content(), view=self)

    @discord.ui.button(label="✏️ Write Message", style=discord.ButtonStyle.primary, row=0)
    async def btn_write(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MessageTextModal(self))

    @discord.ui.button(label="🔗 Add Link Button", style=discord.ButtonStyle.secondary, row=0)
    async def btn_add_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        if len(self.draft["buttons"]) >= 5:
            await interaction.response.send_message("❌ Maximum 5 link buttons reached.", ephemeral=True)
            return
        await interaction.response.send_modal(AddButtonModal(self))

    @discord.ui.button(label="🔗 Remove Link Button", style=discord.ButtonStyle.secondary, row=0)
    async def btn_remove_link(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.draft["buttons"]:
            await interaction.response.send_message("No link buttons to remove.", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(RemoveButtonSelect(self))
        await interaction.response.send_message("Select a link button to remove:", view=view, ephemeral=True)

    @discord.ui.button(label="📤 Send Here", style=discord.ButtonStyle.success, row=1)
    async def btn_send(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.draft["content"]:
            await interaction.response.send_message("❌ Your message is empty. Write something first.", ephemeral=True)
            return

        send_view = discord.ui.View(timeout=None)
        for btn_data in self.draft["buttons"]:
            link_btn = discord.ui.Button(
                label=btn_data["label"],
                url=btn_data["url"],
                emoji=parse_emoji_input(btn_data["emoji"]),
                style=discord.ButtonStyle.link,
            )
            send_view.add_item(link_btn)

        if self.draft["buttons"]:
            await interaction.channel.send(content=self.draft["content"], view=send_view)
        else:
            await interaction.channel.send(content=self.draft["content"])

        await interaction.response.send_message("✅ Message sent!", ephemeral=True)


@tree.command(name="message", description="Envoyer un message texte simple avec des boutons-liens optionnels")
@app_commands.checks.has_permissions(administrator=True)
async def message_cmd(interaction: discord.Interaction):
    view = SimpleMessageView(interaction.user.id)
    await interaction.response.send_message(content=view.preview_content(), view=view, ephemeral=True)


# ─────────────────────────────────────────────
#  COMMANDE /firstclick — premier qui clique gagne
# ─────────────────────────────────────────────

class FirstClickView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.winner_id = None
        self._lock = asyncio.Lock()

    @discord.ui.button(label="🏆 Claim", style=discord.ButtonStyle.success, custom_id="firstclick_claim")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._lock:
            if self.winner_id is not None:
                await interaction.response.send_message("❌ Too late! Someone already claimed it.", ephemeral=True)
                return
            self.winner_id = interaction.user.id

        button.disabled = True
        button.label = "🏆 Claimed!"
        await interaction.response.edit_message(view=self)
        await interaction.channel.send(f"🏆 {interaction.user.mention} won!")


@tree.command(name="firstclick", description="Poster un embed où le premier qui clique gagne")
@app_commands.describe(
    message="Le texte de l'embed",
    role="(Optionnel) Rôle à mentionner pour annoncer le firstclick",
)
@app_commands.checks.has_permissions(administrator=True)
async def firstclick_cmd(interaction: discord.Interaction, message: str, role: discord.Role = None):
    embed = discord.Embed(description=message, color=EMBED_COLOR)
    view = FirstClickView()

    content = role.mention if role else None
    await interaction.channel.send(content=content, embed=embed, view=view)
    await interaction.response.send_message("✅ Posted!", ephemeral=True)


@tree.command(name="editembed", description="Modifier un embed déjà envoyé (répondre au message avec cette commande)")
@app_commands.checks.has_permissions(administrator=True)
async def editembed_cmd(interaction: discord.Interaction):
    if not interaction.channel:
        await interaction.response.send_message("❌ This command must be used in a channel.", ephemeral=True)
        return

    # Discord ne lie pas automatiquement le message répondu à une slash command,
    # donc on demande l'ID/lien du message, ou on regarde le dernier message du bot avec un embed.
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("❌ This command can only be used in a text channel.", ephemeral=True)
        return

    reference = None

    await interaction.response.send_message(
        "Reply to the message you want to edit with `/editembed`, "
        "or paste the message link/ID below within 30 seconds:",
        ephemeral=True,
    )

    def check(m: discord.Message):
        return m.author.id == interaction.user.id and m.channel.id == interaction.channel.id

    try:
        reply_msg = await bot.wait_for("message", check=check, timeout=30)
    except asyncio.TimeoutError:
        await interaction.followup.send("❌ Timed out waiting for a message reference.", ephemeral=True)
        return

    target = None
    if reply_msg.reference and reply_msg.reference.message_id:
        try:
            target = await interaction.channel.fetch_message(reply_msg.reference.message_id)
        except Exception:
            pass

    if not target:
        content = reply_msg.content.strip()
        msg_id = content.split("/")[-1] if "/" in content else content
        try:
            target = await interaction.channel.fetch_message(int(msg_id))
        except Exception:
            target = None

    try:
        await reply_msg.delete()
    except Exception:
        pass

    if not target:
        await interaction.followup.send("❌ Could not find that message. Make sure it's in this channel.", ephemeral=True)
        return

    if target.author.id != bot.user.id or not target.embeds:
        await interaction.followup.send("❌ That message isn't an embed sent by this bot.", ephemeral=True)
        return

    draft = draft_from_message(target)
    view = EmbedBuilderView(interaction.user.id, initial_draft=draft, target_message=target)
    preview = render_draft(draft)
    await interaction.followup.send(
        content="✏️ Editing the selected message. Make your changes, then click **Update Message**.",
        embed=preview,
        view=view,
        ephemeral=True,
    )


@tree.command(name="stick", description="Épingler un message qui se repostera automatiquement quand le salon bouge")
@app_commands.describe(text="Texte simple à stick (laisse vide pour ouvrir l'éditeur d'embed)")
@app_commands.checks.has_permissions(administrator=True)
async def stick_cmd(interaction: discord.Interaction, text: str = None):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("❌ This command can only be used in a text channel.", ephemeral=True)
        return

    # Supprime l'ancien sticky de ce salon s'il existe, pour ne pas laisser d'orphelin
    existing = get_sticky(interaction.channel.id)
    if existing and existing.get("last_message_id"):
        try:
            old_msg = await interaction.channel.fetch_message(existing["last_message_id"])
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    if text:
        sent = await interaction.channel.send(content=text)
        set_sticky(interaction.channel.id, content=text, embed_draft=None, last_message_id=sent.id)
        await interaction.response.send_message("📌 Message stickied in this channel!", ephemeral=True)
        return

    view = EmbedBuilderView(interaction.user.id, stick_mode=True)
    embed = render_draft(view.draft)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@tree.command(name="unstick", description="Retirer le message épinglé du salon actuel")
@app_commands.checks.has_permissions(administrator=True)
async def unstick_cmd(interaction: discord.Interaction):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("❌ This command can only be used in a text channel.", ephemeral=True)
        return

    sticky = get_sticky(interaction.channel.id)
    if not sticky:
        await interaction.response.send_message("⚠️ No sticky message is active in this channel.", ephemeral=True)
        return

    if sticky.get("last_message_id"):
        try:
            old_msg = await interaction.channel.fetch_message(sticky["last_message_id"])
            await old_msg.delete()
        except (discord.NotFound, discord.Forbidden):
            pass

    remove_sticky(interaction.channel.id)
    await interaction.response.send_message("🚫 Sticky message removed from this channel.", ephemeral=True)


# ─────────────────────────────────────────────
#  SYSTÈME DE GIVEAWAY
# ─────────────────────────────────────────────

def format_timedelta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes or not parts:
        parts.append(f"{minutes}m")
    return " ".join(parts)


def build_giveaway_embed(prize: str, winners_count: int, end_timestamp: float, host_id: int, participant_count: int, ended: bool = False) -> discord.Embed:
    if ended:
        title = "🎉 Giveaway Ended"
        color = 0x2F3136
    else:
        title = "🎉 Giveaway"
        color = 0xF1C40F

    embed = discord.Embed(title=title, color=color)
    embed.add_field(name="Prize", value=prize, inline=False)
    embed.add_field(name="Winners", value=str(winners_count), inline=True)
    embed.add_field(name="Participants", value=str(participant_count), inline=True)
    if not ended:
        embed.add_field(name="Ends", value=f"<t:{int(end_timestamp)}:R>", inline=False)
    embed.set_footer(text=f"Hosted by user ID {host_id} • {CREDIT_LINE}")
    return embed


class GiveawayView(discord.ui.View):
    def __init__(self, message_id: int = None):
        super().__init__(timeout=None)
        self.message_id = message_id

    @discord.ui.button(label="🎉 Participate", style=discord.ButtonStyle.success, custom_id="giveaway_participate")
    async def participate(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = interaction.message.id
        giveaway = get_giveaway(msg_id)
        if not giveaway or giveaway.get("ended"):
            await interaction.response.send_message("❌ This giveaway has ended.", ephemeral=True)
            return

        added = add_participant(msg_id, interaction.user.id)
        if not added:
            await interaction.response.send_message("⚠️ You're already entered in this giveaway!", ephemeral=True)
            return

        giveaway = get_giveaway(msg_id)
        embed = build_giveaway_embed(
            giveaway["prize"], giveaway["winners_count"], giveaway["end_timestamp"],
            giveaway["host_id"], len(giveaway["participants"]),
        )
        await interaction.message.edit(embed=embed)
        await interaction.response.send_message("✅ You're entered! Good luck 🍀", ephemeral=True)


async def end_giveaway(message_id: int):
    giveaway = get_giveaway(message_id)
    if not giveaway or giveaway.get("ended"):
        return

    mark_giveaway_ended(message_id)

    channel = bot.get_channel(giveaway["channel_id"])
    if not channel:
        return

    try:
        message = await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden):
        return

    participants = giveaway["participants"]
    winners_count = giveaway["winners_count"]

    if not participants:
        winners = []
    else:
        import random
        winners = random.sample(participants, min(winners_count, len(participants)))

    embed = build_giveaway_embed(
        giveaway["prize"], winners_count, giveaway["end_timestamp"],
        giveaway["host_id"], len(participants), ended=True,
    )

    if winners:
        mentions = ", ".join(f"<@{uid}>" for uid in winners)
        embed.add_field(name="Winner(s)", value=mentions, inline=False)
        announcement = f"🎉 Congratulations {mentions}! You won **{giveaway['prize']}**!"
    else:
        embed.add_field(name="Winner(s)", value="No valid participants.", inline=False)
        announcement = "No one entered this giveaway 😢"

    try:
        await message.edit(embed=embed, view=None)
        await channel.send(announcement)
    except Exception as e:
        print(f"❌ Erreur lors de la fin du giveaway {message_id} : {e}")


async def giveaway_checker_loop():
    await bot.wait_until_ready()
    import time
    while True:
        try:
            active = get_active_giveaways()
            now_unix = time.time()
            for mid, g in active.items():
                if g["end_timestamp"] <= now_unix:
                    await end_giveaway(int(mid))
        except Exception as e:
            print(f"❌ Erreur dans giveaway_checker_loop : {e}")
        await asyncio.sleep(15)


@tree.command(name="giveaway", description="Lancer un giveaway avec un bouton de participation")
@app_commands.describe(
    prize="Le lot à gagner",
    duration="Durée (ex: 30m, 2h, 1d)",
    winners="Nombre de gagnants",
)
@app_commands.checks.has_permissions(administrator=True)
async def giveaway_cmd(interaction: discord.Interaction, prize: str, duration: str, winners: int = 1):
    if not isinstance(interaction.channel, discord.TextChannel):
        await interaction.response.send_message("❌ This command can only be used in a text channel.", ephemeral=True)
        return

    if winners < 1 or winners > 50:
        await interaction.response.send_message("❌ Winners must be between 1 and 50.", ephemeral=True)
        return

    seconds = parse_duration(duration)
    if seconds is None or seconds < 10:
        await interaction.response.send_message(
            "❌ Invalid duration. Use formats like `30m`, `2h`, `1d`.", ephemeral=True
        )
        return

    import time
    end_timestamp = time.time() + seconds

    embed = build_giveaway_embed(prize, winners, end_timestamp, interaction.user.id, 0)
    view = GiveawayView()

    await interaction.response.send_message("🎉 Giveaway started!", ephemeral=True)
    message = await interaction.channel.send(embed=embed, view=view)

    create_giveaway(message.id, interaction.channel.id, prize, winners, end_timestamp, interaction.user.id)


def parse_duration(text: str):
    """Parse '30m', '2h', '1d', '90s' en secondes. Retourne None si invalide."""
    text = text.strip().lower()
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if not text:
        return None
    unit = text[-1]
    if unit not in units:
        return None
    try:
        value = float(text[:-1])
    except ValueError:
        return None
    return value * units[unit]


# ─────────────────────────────────────────────
#  SYSTÈME DE COMMANDE (/order)
# ─────────────────────────────────────────────

ORDER_EMBED_TITLE = "🛒 Order a Weight"
ORDER_EMBED_DESCRIPTION = (
    "Click the button below to start your order.\n\n"
    "You'll be asked for your contact info and the model you're looking for, "
    "then you'll receive a payment link and next steps."
)


class OrderDetailsModal(discord.ui.Modal, title="Order Details"):
    contact_input = discord.ui.TextInput(
        label="Your Discord username or email",
        max_length=200,
        required=True,
    )
    model_input = discord.ui.TextInput(
        label="Which model are you looking for?",
        max_length=200,
        required=True,
        placeholder="e.g. XyCubValorantV2",
    )

    async def on_submit(self, interaction: discord.Interaction):
        order_id = create_order(
            buyer_id=interaction.user.id,
            buyer_contact=str(self.contact_input.value),
            model=str(self.model_input.value),
        )

        embed = discord.Embed(
            title="💳 Complete Your Payment",
            description=(
                f"Thanks! Please send payment for **{self.model_input.value}** using the link below.\n\n"
                f"Once you've paid, click **I've Paid** and enter the email you used for PayPal."
            ),
            color=0xF1C40F,
        )

        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Pay with PayPal", url=PAYPAL_LINK, style=discord.ButtonStyle.link))
        view.add_item(PaidButton(order_id))

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class PaidButton(discord.ui.Button):
    def __init__(self, order_id: str):
        super().__init__(label="✅ I've Paid", style=discord.ButtonStyle.success, custom_id=f"paid_{order_id}")
        self.order_id = order_id

    async def callback(self, interaction: discord.Interaction):
        order = get_order(self.order_id)
        if not order:
            await interaction.response.send_message("❌ Order not found.", ephemeral=True)
            return
        await interaction.response.send_modal(PaymentEmailModal(self.order_id))


class PaymentEmailModal(discord.ui.Modal, title="Confirm Payment"):
    email_input = discord.ui.TextInput(
        label="Email used for PayPal payment",
        max_length=200,
        required=True,
        placeholder="you@example.com",
    )

    def __init__(self, order_id: str):
        super().__init__()
        self.order_id = order_id

    async def on_submit(self, interaction: discord.Interaction):
        order = get_order(self.order_id)
        if not order:
            await interaction.response.send_message("❌ Order not found.", ephemeral=True)
            return

        update_order(self.order_id, payment_email=str(self.email_input.value), status="awaiting_review")

        await interaction.response.send_message(
            "✅ Thanks! Your payment is being reviewed. We'll notify you once it's confirmed.",
            ephemeral=True,
        )

        # Notifier le staff
        staff_channel = bot.get_channel(STAFF_ORDER_CHANNEL_ID) if STAFF_ORDER_CHANNEL_ID else None
        if not staff_channel:
            return

        staff_embed = discord.Embed(title="🛒 New Order — Awaiting Review", color=0xF1C40F)
        staff_embed.add_field(name="Buyer", value=f"<@{order['buyer_id']}>", inline=True)
        staff_embed.add_field(name="Contact", value=order["buyer_contact"], inline=True)
        staff_embed.add_field(name="Model", value=order["model"], inline=False)
        staff_embed.add_field(name="Payment Email", value=str(self.email_input.value), inline=False)
        staff_embed.set_footer(text=f"Order ID: {self.order_id}")

        staff_view = OrderReviewView(self.order_id)
        staff_msg = await staff_channel.send(embed=staff_embed, view=staff_view)
        update_order(self.order_id, staff_message_id=staff_msg.id)


class OrderReviewView(discord.ui.View):
    def __init__(self, order_id: str):
        super().__init__(timeout=None)
        self.order_id = order_id

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success, custom_id="order_accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        order_id = self.order_id
        order = get_order(order_id)
        if not order:
            await interaction.response.send_message("❌ Order not found.", ephemeral=True)
            return

        update_order(order_id, status="accepted")

        embed = interaction.message.embeds[0]
        embed.color = 0x57F287
        embed.title = "✅ Order Accepted — In Progress"

        complete_view = discord.ui.View(timeout=None)
        complete_view.add_item(MarkDeliveredButton(order_id))

        await interaction.message.edit(embed=embed, view=complete_view)
        await interaction.response.send_message("✅ Order marked as accepted.", ephemeral=True)

    @discord.ui.button(label="❌ Reject", style=discord.ButtonStyle.danger, custom_id="order_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        order_id = self.order_id
        order = get_order(order_id)
        if not order:
            await interaction.response.send_message("❌ Order not found.", ephemeral=True)
            return

        update_order(order_id, status="rejected")

        embed = interaction.message.embeds[0]
        embed.color = 0xED4245
        embed.title = "❌ Order Rejected"

        await interaction.message.edit(embed=embed, view=None)
        await interaction.response.send_message("🚫 Order marked as rejected.", ephemeral=True)

        buyer = bot.get_user(order["buyer_id"])
        if buyer:
            try:
                await buyer.send(
                    f"❌ Your order for **{order['model']}** could not be confirmed. "
                    f"Please open a ticket if you believe this is a mistake."
                )
            except discord.Forbidden:
                pass


class MarkDeliveredButton(discord.ui.Button):
    def __init__(self, order_id: str):
        super().__init__(label="📦 Mark as Delivered", style=discord.ButtonStyle.primary, custom_id=f"deliver_{order_id}")
        self.order_id = order_id

    async def callback(self, interaction: discord.Interaction):
        order = get_order(self.order_id)
        if not order:
            await interaction.response.send_message("❌ Order not found.", ephemeral=True)
            return

        update_order(self.order_id, status="delivered")

        embed = interaction.message.embeds[0]
        embed.color = 0x2F3136
        embed.title = "📦 Order Delivered"

        await interaction.message.edit(embed=embed, view=None)
        await interaction.response.send_message("📦 Order marked as delivered.", ephemeral=True)

        buyer = bot.get_user(order["buyer_id"])
        if buyer:
            try:
                await buyer.send(
                    f"📦 Your order for **{order['model']}** has been delivered! "
                    f"The model has been added / access has been granted. Enjoy!"
                )
            except discord.Forbidden:
                pass


class OrderStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🛒 Order Now", style=discord.ButtonStyle.success, custom_id="order_start")
    async def order_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OrderDetailsModal())


@tree.command(name="order", description="Poster le panneau de commande pour acheter une weight")
@app_commands.checks.has_permissions(administrator=True)
async def order_cmd(interaction: discord.Interaction):
    if not STAFF_ORDER_CHANNEL_ID:
        await interaction.response.send_message(
            "⚠️ STAFF_ORDER_CHANNEL_ID is not configured. Set it in Railway variables first.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(title=ORDER_EMBED_TITLE, description=ORDER_EMBED_DESCRIPTION, color=EMBED_COLOR)
    view = OrderStartView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Order panel posted!", ephemeral=True)


# ─────────────────────────────────────────────
#  LANCEMENT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
