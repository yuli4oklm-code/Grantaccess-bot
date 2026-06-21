"""
Bot Discord séparé : commande Stripe + livraison automatique sur Winsight via Playwright.

Ce bot NE TOUCHE PAS au bot principal (tickets/templates/giveaways/etc).
C'est un projet complètement indépendant, avec son propre token Discord et son propre déploiement Railway.
"""

import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import json
import threading
import time

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

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
ORDERS_FILE = os.path.join(DATA_DIR, "winsight_orders.json")
PRODUCTS_FILE = os.path.join(DATA_DIR, "winsight_products.json")

EMBED_COLOR = 0x2F3136

# Produits disponibles : nom du modèle -> {"winsight": prix_centimes, "enginex": prix_centimes}
# Une plateforme absente du dict pour un modèle = non disponible sur cette plateforme
DEFAULT_PRODUCTS = {
    "XyCubValorantV2": {"winsight": 2000},
}

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
    # Rétrocompatibilité avec les anciens formats de stockage
    normalized = {}
    for name, value in data.items():
        if isinstance(value, dict) and "price" in value and "platform" in value:
            # Ancien format {"price": ..., "platform": ...} -> nouveau format
            normalized[name] = {value["platform"]: value["price"]}
        elif isinstance(value, dict):
            # Déjà au nouveau format {"winsight": price, "enginex": price}
            normalized[name] = value
        else:
            # Très ancien format : juste un nombre = prix sur winsight
            normalized[name] = {"winsight": value}
    return normalized


def create_order(order_id: str, buyer_id: int, buyer_contact: str, model: str, platform: str, stripe_session_id: str):
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


def get_order(order_id: str):
    return load_orders().get(order_id)


def get_order_by_session(session_id: str):
    data = load_orders()
    for oid, o in data.items():
        if o.get("stripe_session_id") == session_id:
            return oid, o
    return None, None


def update_order(order_id: str, **kwargs):
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

# Boucle asyncio principale du bot, utilisée pour planifier des coroutines
# depuis le thread Flask (qui tourne séparément)
main_loop = None


ORDER_EMBED_TITLE = "🛒 Order a Weight"
ORDER_EMBED_DESCRIPTION = (
    "Click the button below to order. You'll enter your contact info and the model "
    "you want, then pay securely via Stripe. Delivery is automatic after payment."
)


class ContactOnlyModal(discord.ui.Modal):
    def __init__(self, model_name: str, platform: str):
        if platform == "enginex":
            label = "Your email (used on EngineX)"
        else:
            label = "Your Discord username or ID (used on Winsight)"

        super().__init__(title="Order Details")
        self.model_name = model_name
        self.platform = platform
        self.contact_input = discord.ui.TextInput(label=label, max_length=200, required=True)
        self.add_item(self.contact_input)

    async def on_submit(self, interaction: discord.Interaction):
        products = load_products()

        if self.model_name not in products or self.platform not in products[self.model_name]:
            await interaction.response.send_message(
                f"❌ This model/platform combination is no longer available. Please start over.",
                ephemeral=True,
            )
            return

        price_cents = products[self.model_name][self.platform]
        order_id = f"{interaction.user.id}_{int(time.time() * 1000)}"

        try:
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": f"Weight: {self.model_name} ({self.platform.capitalize()})"},
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
                f"**Model:** {self.model_name}\n**Platform:** {self.platform.capitalize()}\n"
                f"**Price:** ${price_cents / 100:.2f}\n\n"
                f"Click below to pay securely via Stripe. "
                f"Once paid, your weight will be delivered automatically — no further action needed!"
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
            discord.SelectOption(label=platform.capitalize(), description=f"${price / 100:.2f}", value=platform)
            for platform, price in available_platforms.items()
        ]
        super().__init__(placeholder="Choose a platform...", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ContactOnlyModal(self.model_name, self.values[0]))


class ModelSelect(discord.ui.Select):
    def __init__(self):
        products = load_products()
        options = []
        for name, platforms in products.items():
            if not platforms:
                continue
            min_price = min(platforms.values())
            platform_count = len(platforms)
            if platform_count > 1:
                desc = f"From ${min_price / 100:.2f} • {platform_count} platforms"
            else:
                only_platform = next(iter(platforms))
                desc = f"${min_price / 100:.2f} • {only_platform.capitalize()}"
            options.append(discord.SelectOption(label=name, description=desc))
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
            # Une seule plateforme dispo : on saute direct au modal de contact
            only_platform = next(iter(available_platforms))
            await interaction.response.send_modal(ContactOnlyModal(model_name, only_platform))
        else:
            # Plusieurs plateformes dispo : on demande au client de choisir
            platform_view = discord.ui.View(timeout=120)
            platform_view.add_item(PlatformSelect(model_name, available_platforms))
            await interaction.response.send_message(
                f"**{model_name}** is available on multiple platforms. Which one would you like?",
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


@bot.event
async def on_ready():
    await tree.sync()
    bot.add_view(OrderStartView())
    print(f"✅ Bot connecté en tant que {bot.user} (ID: {bot.user.id})")

    if ORDER_PANEL_CHANNEL_ID:
        channel = bot.get_channel(ORDER_PANEL_CHANNEL_ID)
        if channel:
            embed = discord.Embed(title=ORDER_EMBED_TITLE, description=ORDER_EMBED_DESCRIPTION, color=EMBED_COLOR)
            await channel.send(embed=embed, view=OrderStartView())
            print(f"📌 Order panel posted in #{channel.name}")


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
async def addproduct_cmd(interaction: discord.Interaction, model: str, price_usd: float, platform: app_commands.Choice[str]):
    products = load_products()
    if model not in products:
        products[model] = {}
    products[model][platform.value] = int(price_usd * 100)
    save_json(PRODUCTS_FILE, products)
    await interaction.response.send_message(
        f"✅ Product **{model}** set to ${price_usd:.2f} on **{platform.name}**.", ephemeral=True
    )


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
                f"🚫 Removed **{model}** from **{platform.name}**.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"⚠️ **{model}** is not available on **{platform.name}**.", ephemeral=True
            )
    else:
        del products[model]
        save_json(PRODUCTS_FILE, products)
        await interaction.response.send_message(f"🚫 Removed **{model}** from all platforms.", ephemeral=True)


# ─────────────────────────────────────────────
#  AUTOMATISATION WINSIGHT (Playwright)
# ─────────────────────────────────────────────

async def winsight_grant(discord_id: str, model_name: str) -> tuple[bool, str]:
    print(f"[Winsight] Starting grant for discord_id={discord_id}, model={model_name}")
    try:
        async with async_playwright() as p:
            print("[Winsight] Launching browser...")
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            print(f"[Winsight] Navigating to {WINSIGHT_URL}")
            await page.goto(WINSIGHT_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)
            print(f"[Winsight] Page loaded, title: {await page.title()}")

            login_input = await page.query_selector("input[type='text']")
            if login_input:
                print("[Winsight] Login form detected, logging in...")
                await page.fill("input[type='text']", WINSIGHT_USERNAME)
                await page.fill("input[type='password']", WINSIGHT_PASSWORD)

                clicked = await page.evaluate("""
                    () => {
                        const buttons = document.querySelectorAll("button");
                        for (const btn of buttons) {
                            if (btn.textContent.toUpperCase().includes("SIGN IN")) {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
                    }
                """)
                print(f"[Winsight] Sign in button clicked via JS: {clicked}")

                if not clicked:
                    # Fallback : essaie un vrai clic Playwright sur le texte
                    await page.click("text=SIGN IN", timeout=10000)

                # Attend un peu plus longtemps pour laisser la connexion s'effectuer (appel API + redirection)
                await asyncio.sleep(3)
                await page.wait_for_load_state("networkidle", timeout=30000)
                print(f"[Winsight] Logged in, new title: {await page.title()}")

                # Vérifie qu'on est bien sorti de l'écran de login
                still_login = await page.query_selector("input[type='password']")
                if still_login:
                    print("[Winsight] WARNING: still on login screen after sign-in attempt. Credentials may be wrong, or extra wait needed.")
                    page_text_debug = await page.evaluate("() => document.body.innerText.substring(0, 500)")
                    print(f"[Winsight] Page text after login attempt: {page_text_debug}")
            else:
                print("[Winsight] No login form found (already logged in?)")

            print(f"[Winsight] Searching for model '{model_name}' on page...")

            # Étape 1 : localiser l'élément JS contenant le nom du modèle, et obtenir un identifiant unique
            match_info = await page.evaluate(f"""
                () => {{
                    const modelName = "{model_name}".toLowerCase();
                    const allElements = document.querySelectorAll("*");
                    let matchEl = null;

                    for (const el of allElements) {{
                        let directText = "";
                        for (const node of el.childNodes) {{
                            if (node.nodeType === Node.TEXT_NODE) {{
                                directText += node.textContent;
                            }}
                        }}
                        if (directText.toLowerCase().includes(modelName)) {{
                            matchEl = el;
                            break;
                        }}
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
                            if (btn.textContent.toUpperCase().includes("SHARE")) {{
                                shareBtn = btn;
                                break;
                            }}
                        }}

                        if (input && shareBtn) {{
                            // Marque l'input et le bouton avec des attributs uniques pour les retrouver via Playwright
                            input.setAttribute("data-bot-target-input", "true");
                            shareBtn.setAttribute("data-bot-target-button", "true");
                            return {{
                                status: "found",
                                matchedText: matchEl.textContent.trim().substring(0, 100)
                            }};
                        }}
                    }}

                    return {{ status: "container_not_found", matchedText: matchEl.textContent.trim().substring(0, 100) }};
                }}
            """)
            print(f"[Winsight] Match info: {match_info}")
            if "matchedText" in match_info:
                print(f"[Winsight] Matched element text: '{match_info['matchedText']}'")

            if match_info["status"] == "found":
                # Étape 2 : utiliser Playwright pour remplir le champ comme un vrai utilisateur (compatible React)
                input_locator = page.locator("[data-bot-target-input='true']")
                await input_locator.click()
                await input_locator.fill("")
                await input_locator.type(discord_id, delay=30)
                await asyncio.sleep(0.5)

                filled_value = await input_locator.input_value()
                print(f"[Winsight] Input filled, current value: '{filled_value}'")

                share_button = page.locator("[data-bot-target-button='true']")
                await share_button.click()
                print("[Winsight] Share button clicked via Playwright locator.")

                found = "clicked"
            else:
                found = match_info["status"]

            print(f"[Winsight] Search result: {found}")

            if found == "not_found":
                # Dump le HTML pour debug : on cherche manuellement la zone qui contient "XyCub" ou "Weights"
                debug_html = await page.evaluate("""
                    () => {
                        const body = document.body.innerText;
                        return body.substring(0, 2000);
                    }
                """)
                print(f"[Winsight] DEBUG page text content:\\n{debug_html}")

            await asyncio.sleep(2)
            await browser.close()
            print("[Winsight] Browser closed.")

            if found == "clicked":
                return True, f"Access granted to {discord_id} for {model_name} on Winsight."
            elif found == "container_not_found":
                return False, f"Found model name '{model_name}' but couldn't locate its input/share button container."
            else:
                return False, f"Could not find model '{model_name}' on Winsight."

    except Exception as e:
        print(f"[Winsight] EXCEPTION: {str(e)}")
        return False, f"Error: {str(e)}"


async def enginex_grant(email: str, model_name: str) -> tuple[bool, str]:
    print(f"[EngineX] Starting grant for email={email}, model={model_name}")
    try:
        async with async_playwright() as p:
            print("[EngineX] Launching browser...")
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()

            print(f"[EngineX] Navigating to login page {ENGINEX_LOGIN_URL}")
            await page.goto(ENGINEX_LOGIN_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)

            print("[EngineX] Filling login form...")
            await page.locator("input[type='email'], input[name='email']").first.fill(ENGINEX_USERNAME)
            await page.locator("input[type='password']").first.fill(ENGINEX_PASSWORD)

            clicked = await page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll("button");
                    for (const btn of buttons) {
                        if (btn.textContent.toUpperCase().includes("SIGN IN")) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            print(f"[EngineX] Sign in button clicked via JS: {clicked}")
            if not clicked:
                await page.click("text=Sign in", timeout=10000)

            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle", timeout=30000)
            print(f"[EngineX] Logged in, current URL: {page.url}")

            print(f"[EngineX] Navigating to entitlements page {ENGINEX_ENTITLEMENTS_URL}")
            await page.goto(ENGINEX_ENTITLEMENTS_URL, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=30000)

            # Cliquer sur "+ Grant Access"
            grant_clicked = await page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll("button");
                    for (const btn of buttons) {
                        if (btn.textContent.toUpperCase().includes("GRANT ACCESS")) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            print(f"[EngineX] 'Grant Access' button clicked: {grant_clicked}")
            if not grant_clicked:
                await browser.close()
                return False, "Could not find 'Grant Access' button on entitlements page."

            await asyncio.sleep(1)

            # Remplir le champ de recherche utilisateur
            search_input = page.locator("input[placeholder*='email'], input[placeholder*='Discord'], input[placeholder*='username']").first
            await search_input.click()
            await search_input.fill("")
            await search_input.type(email, delay=30)
            print(f"[EngineX] Typed email into search field: {email}")

            await asyncio.sleep(1.5)  # Laisse le temps à la recherche de remonter un résultat

            # Debug : dump la structure HTML autour du champ de recherche pour identifier le vrai résultat
            debug_dropdown_html = await page.evaluate("""
                () => {
                    const input = document.querySelector("input[placeholder*='email'], input[placeholder*='Discord'], input[placeholder*='username']");
                    if (!input) return "input_not_found";
                    let container = input.parentElement;
                    for (let i = 0; i < 3; i++) {
                        if (!container) break;
                        container = container.parentElement;
                    }
                    return container ? container.outerHTML.substring(0, 3000) : "container_not_found";
                }
            """)
            print(f"[EngineX] DEBUG dropdown area HTML:\\n{debug_dropdown_html}")

            # Cliquer sur le résultat de recherche : on cible précisément le <div> cliquable
            # qui a un style "cursor: pointer", comme observé dans le HTML réel du dropdown
            result_clicked = False
            try:
                result_locator = page.locator("div[style*='cursor: pointer']").first
                await result_locator.click(timeout=5000)
                result_clicked = True
                print("[EngineX] Search result clicked via cursor:pointer div locator.")
            except Exception as e:
                print(f"[EngineX] cursor:pointer click failed: {e}")

                try:
                    result_locator = page.locator(f"*:not(input):has-text('{email}')").last
                    parent_locator = result_locator.locator("xpath=..")
                    await parent_locator.click(timeout=5000)
                    result_clicked = True
                    print("[EngineX] Search result clicked via parent element fallback.")
                except Exception as e2:
                    print(f"[EngineX] Parent click fallback also failed: {e2}")

            print(f"[EngineX] Search result clicked: {result_clicked}")

            if not result_clicked:
                await browser.close()
                return False, f"Could not find user matching email '{email}' in search results."

            await asyncio.sleep(0.8)

            # Vérification : le champ de recherche devrait maintenant être vide ou remplacé par une confirmation
            search_field_state = await page.evaluate("""
                () => {
                    const input = document.querySelector("input[placeholder*='email'], input[placeholder*='Discord'], input[placeholder*='username']");
                    return input ? input.value : "field_gone";
                }
            """)
            print(f"[EngineX] Search field state after selection: '{search_field_state}'")

            # Sélectionner le bon modèle dans le dropdown via Playwright (compatible React)
            model_label = await page.evaluate(f"""
                () => {{
                    const modelName = "{model_name}".toLowerCase();
                    const selects = document.querySelectorAll("select");
                    for (const select of selects) {{
                        for (const option of select.options) {{
                            if (option.textContent.toLowerCase().includes(modelName)) {{
                                return option.textContent;
                            }}
                        }}
                    }}
                    return null;
                }}
            """)
            print(f"[EngineX] Found dropdown option label: {model_label}")

            model_selected = False
            if model_label:
                try:
                    select_locator = page.locator("select").first
                    await select_locator.select_option(label=model_label)
                    model_selected = True
                except Exception as e:
                    print(f"[EngineX] select_option failed: {e}")

            print(f"[EngineX] Model '{model_name}' selected in dropdown: {model_selected}")

            if not model_selected:
                await browser.close()
                return False, f"Could not find model '{model_name}' in the dropdown."

            await asyncio.sleep(0.5)

            # Vérifie d'abord si le bouton final est désactivé (signe que le user n'a pas été sélectionné)
            button_disabled = await page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll("button[type='submit']");
                    for (const btn of buttons) {
                        if (btn.textContent.trim().toUpperCase() === "GRANT ACCESS") {
                            return btn.disabled;
                        }
                    }
                    return null;
                }
            """)
            print(f"[EngineX] Grant Access button disabled state before click: {button_disabled}")

            # Cliquer sur le bouton final "Grant Access" dans le modal
            final_clicked = await page.evaluate("""
                () => {
                    const buttons = document.querySelectorAll("button");
                    for (const btn of buttons) {
                        if (btn.textContent.trim().toUpperCase() === "GRANT ACCESS") {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            print(f"[EngineX] Final 'Grant Access' click: {final_clicked}")

            await asyncio.sleep(2)

            # Vérification : le modal "Grant Model Access" devrait avoir disparu si l'action a réussi
            modal_still_open = await page.evaluate("""
                () => {
                    const body = document.body.innerText;
                    return body.includes("Grant Model Access") && body.includes("Select a user and a model");
                }
            """)
            print(f"[EngineX] Modal still open after final click: {modal_still_open}")

            await browser.close()
            print("[EngineX] Browser closed.")

            if final_clicked and not modal_still_open:
                return True, f"Access granted to {email} for {model_name} on EngineX."
            elif final_clicked and modal_still_open:
                return False, "Clicked Grant Access but modal is still open — action likely did not register (form may be incomplete or validation failed)."
            else:
                return False, "Could not click final 'Grant Access' button."

    except Exception as e:
        print(f"[EngineX] EXCEPTION: {str(e)}")
        return False, f"Error: {str(e)}"


async def process_paid_order(order_id: str):
    print(f"[Order] Processing paid order: {order_id}")
    order = get_order(order_id)
    if not order:
        print(f"❌ Order {order_id} not found.")
        return

    print(f"[Order] Order data: {order}")
    update_order(order_id, status="processing")

    platform = order.get("platform", "winsight")
    print(f"[Order] Routing to platform: {platform}")

    if platform == "enginex":
        success, message = await enginex_grant(order["buyer_contact"], order["model"])
    else:
        success, message = await winsight_grant(order["buyer_contact"], order["model"])

    print(f"[Order] Grant result: success={success}, message={message}")

    if success:
        update_order(order_id, status="delivered")
    else:
        update_order(order_id, status="failed")

    # Notifie le salon staff
    if STAFF_CHANNEL_ID:
        channel = bot.get_channel(STAFF_CHANNEL_ID)
        if channel:
            color = 0x57F287 if success else 0xED4245
            title = "✅ Order Delivered Automatically" if success else "❌ Auto-Delivery Failed — Manual Action Needed"
            embed = discord.Embed(title=title, color=color)
            embed.add_field(name="Buyer", value=f"<@{order['buyer_id']}>", inline=True)
            embed.add_field(name="Contact", value=order["buyer_contact"], inline=True)
            embed.add_field(name="Model", value=order["model"], inline=False)
            embed.add_field(name="Details", value=message, inline=False)
            embed.set_footer(text=f"Order ID: {order_id}")
            await channel.send(embed=embed)

    # Notifie le client en DM
    buyer = bot.get_user(order["buyer_id"])
    if buyer:
        try:
            if success:
                await buyer.send(
                    f"✅ Your payment was received and **{order['model']}** has been added to your Winsight account!"
                )
            else:
                await buyer.send(
                    f"⚠️ Your payment for **{order['model']}** was received, but automatic delivery failed. "
                    f"Our team has been notified and will resolve this manually shortly."
                )
        except discord.Forbidden:
            pass


# ─────────────────────────────────────────────
#  SERVEUR WEB FLASK (webhook Stripe)
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

        # session peut être un dict classique OU un objet Stripe selon le contexte;
        # on récupère metadata de façon robuste dans les deux cas.
        metadata = session["metadata"] if "metadata" in session else {}
        order_id = metadata["order_id"] if metadata and "order_id" in metadata else None

        print(f"[Webhook] checkout.session.completed received, order_id={order_id}, main_loop set={main_loop is not None}")

        if order_id and main_loop:
            asyncio.run_coroutine_threadsafe(process_paid_order(order_id), main_loop)
            print(f"[Webhook] Scheduled process_paid_order for {order_id}")
        else:
            print(f"[Webhook] NOT scheduled — order_id={order_id}, main_loop={main_loop}")

    return jsonify({"status": "ok"}), 200


@flask_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


def run_flask():
    port = int(os.environ.get("PORT", 5000))
    flask_app.run(host="0.0.0.0", port=port)


# ─────────────────────────────────────────────
#  VÉRIFICATION D'ACCÈS (lecture seule)
# ─────────────────────────────────────────────

async def winsight_check(discord_id: str, model_name: str) -> tuple[bool, str]:
    print(f"[Winsight Check] Checking access for discord_id={discord_id}, model={model_name}")
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
                            if (btn.textContent.toUpperCase().includes("SIGN IN")) {
                                btn.click();
                                return true;
                            }
                        }
                        return false;
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
                            if (node.nodeType === Node.TEXT_NODE) {{
                                directText += node.textContent;
                            }}
                        }}
                        if (directText.toLowerCase().includes(modelName)) {{
                            matchEl = el;
                            break;
                        }}
                    }}

                    if (!matchEl) return null;

                    let parent = matchEl;
                    for (let i = 0; i < 12; i++) {{
                        parent = parent.parentElement;
                        if (!parent) break;
                        if (parent.textContent.includes(discordId)) {{
                            return true;
                        }}
                        const input = parent.querySelector("input[placeholder*='username'], input[placeholder*='customer']");
                        if (input) {{
                            // On a atteint la carte du modèle sans trouver l'ID dans son texte
                            return false;
                        }}
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
                return False, f"⚠️ Could not determine access (model not found or page structure unclear)."

    except Exception as e:
        print(f"[Winsight Check] EXCEPTION: {str(e)}")
        return False, f"Error checking Winsight: {str(e)}"


async def enginex_check(email: str, model_name: str) -> tuple[bool, str]:
    print(f"[EngineX Check] Checking access for email={email}, model={model_name}")
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
                        if (btn.textContent.toUpperCase().includes("SIGN IN")) {
                            btn.click();
                            return true;
                        }
                    }
                    return false;
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
                return False, f"⚠️ Could not find {email} in the entitlements list."

    except Exception as e:
        print(f"[EngineX Check] EXCEPTION: {str(e)}")
        return False, f"Error checking EngineX: {str(e)}"


# ─────────────────────────────────────────────
#  COMMANDES MANUELLES (/grantaccess, /checkaccess)
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
#  COMMANDES MANUELLES (/grantaccess, /checkaccess) — avec autocomplete
# ─────────────────────────────────────────────

async def model_autocomplete(interaction: discord.Interaction, current: str):
    products = load_products()
    matches = [name for name in products.keys() if current.lower() in name.lower()]
    return [app_commands.Choice(name=name, value=name) for name in matches[:25]]


def get_platforms_for_model(model_name: str) -> list:
    products = load_products()
    return list(products.get(model_name, {}).keys())


@tree.command(name="grantaccess", description="Donner manuellement l'accès à un modèle (paiement hors Stripe)")
@app_commands.describe(
    model="Le modèle à donner",
    platform="La plateforme (Winsight ou EngineX)",
    contact="ID Discord (Winsight) ou email (EngineX) du client",
)
@app_commands.autocomplete(model=model_autocomplete)
@app_commands.choices(platform=platform_choices)
@app_commands.checks.has_permissions(administrator=True)
async def grantaccess_cmd(interaction: discord.Interaction, model: str, platform: app_commands.Choice[str], contact: str):
    available = get_platforms_for_model(model)
    if platform.value not in available:
        await interaction.response.send_message(
            f"❌ **{model}** is not configured on **{platform.name}**. Available platforms: {', '.join(available) or 'none'}",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"⏳ Granting **{model}** on **{platform.name}** to `{contact}`...", ephemeral=True
    )

    async def run_grant():
        if platform.value == "enginex":
            success, message = await enginex_grant(contact, model)
        else:
            success, message = await winsight_grant(contact, model)

        embed = discord.Embed(
            title="✅ Manual Grant Complete" if success else "❌ Manual Grant Failed",
            description=message,
            color=0x57F287 if success else 0xED4245,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    asyncio.create_task(run_grant())


@tree.command(name="checkaccess", description="Vérifier si un client a déjà accès à un modèle")
@app_commands.describe(
    model="Le modèle à vérifier",
    platform="La plateforme (Winsight ou EngineX)",
    contact="ID Discord (Winsight) ou email (EngineX) du client",
)
@app_commands.autocomplete(model=model_autocomplete)
@app_commands.choices(platform=platform_choices)
@app_commands.checks.has_permissions(administrator=True)
async def checkaccess_cmd(interaction: discord.Interaction, model: str, platform: app_commands.Choice[str], contact: str):
    available = get_platforms_for_model(model)
    if platform.value not in available:
        await interaction.response.send_message(
            f"❌ **{model}** is not configured on **{platform.name}**. Available platforms: {', '.join(available) or 'none'}",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"⏳ Checking **{model}** on **{platform.name}** for `{contact}`...", ephemeral=True
    )

    async def run_check():
        if platform.value == "enginex":
            success, message = await enginex_check(contact, model)
        else:
            success, message = await winsight_check(contact, model)

        embed = discord.Embed(title="🔍 Access Check Result", description=message, color=0x5865F2)
        await interaction.followup.send(embed=embed, ephemeral=True)

    asyncio.create_task(run_check())


# ─────────────────────────────────────────────
#  LANCEMENT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    main_loop.run_until_complete(bot.start(BOT_TOKEN))
