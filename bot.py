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
VOUCH_CHANNEL_ID = int(os.environ.get("VOUCH_CHANNEL_ID", "0")) or None

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
ORDERS_FILE = os.path.join(DATA_DIR, "winsight_orders.json")
PRODUCTS_FILE = os.path.join(DATA_DIR, "winsight_products.json")
PIPELINES_FILE = os.path.join(DATA_DIR, "winsight_pipelines.json")

EMBED_COLOR = 0x2F3136

# Produits disponibles : nom du modèle -> {"winsight": prix_centimes, "enginex": prix_centimes}
DEFAULT_PRODUCTS = {
    "XyCubValorantV2": {"winsight": 2000},
}
DEFAULT_PIPELINES = {}

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


ORDER_EMBED_TITLE = "🛒 Order a Weight"
ORDER_EMBED_DESCRIPTION = (
    "Click the button below to order. You'll enter your contact info and the model "
    "you want, then pay securely via Stripe. Delivery is automatic after payment."
)


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
                        "currency": "eur",
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
                f"**Price:** €{price_cents / 100:.2f}\n\n"
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
            discord.SelectOption(label=platform.capitalize(), description=f"€{price / 100:.2f}", value=platform)
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
            platform_count = len(platforms)
            if platform_count > 1:
                desc = f"From €{min_price / 100:.2f} • {platform_count} platforms"
            else:
                only_platform = next(iter(platforms))
                desc = f"€{min_price / 100:.2f} • {only_platform.capitalize()}"
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
            only_platform = next(iter(available_platforms))
            await interaction.response.send_modal(ContactOnlyModal(model_name, only_platform))
        else:
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
async def addproduct_cmd(interaction: discord.Interaction, model: str, price_eur: float, platform: app_commands.Choice[str]):
    products = load_products()
    if model not in products:
        products[model] = {}
    products[model][platform.value] = int(price_eur * 100)
    save_json(PRODUCTS_FILE, products)
    await interaction.response.send_message(
        f"✅ Product **{model}** set to €{price_eur:.2f} on **{platform.name}**.", ephemeral=True
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
        lines = "\n".join(
            f"● **{platform.capitalize()}** — €{price / 100:.2f}"
            for platform, price in platforms.items()
        )
        embed.add_field(name=name, value=lines, inline=False)

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
                    await page.click("text=SIGN IN", timeout=10000)

                await asyncio.sleep(3)
                await page.wait_for_load_state("networkidle", timeout=30000)
                print(f"[Winsight] Logged in, new title: {await page.title()}")

                still_login = await page.query_selector("input[type='password']")
                if still_login:
                    print("[Winsight] WARNING: still on login screen after sign-in attempt.")
                    page_text_debug = await page.evaluate("() => document.body.innerText.substring(0, 500)")
                    print(f"[Winsight] Page text after login attempt: {page_text_debug}")
            else:
                print("[Winsight] No login form found (already logged in?)")

            print(f"[Winsight] Searching for model '{model_name}' on page...")

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

            if match_info["status"] == "found":
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
                debug_html = await page.evaluate("""
                    () => {
                        const body = document.body.innerText;
                        return body.substring(0, 2000);
                    }
                """)
                print(f"[Winsight] DEBUG page text content:\n{debug_html}")

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


async def winsight_grant_pipeline(discord_id: str, pipeline_site_name: str) -> tuple[bool, str]:
    success, message = await winsight_grant(discord_id, pipeline_site_name)
    if success:
        return True, f"Pipeline access granted to {discord_id} for {pipeline_site_name} on Winsight."
    return False, message


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

            search_input = page.locator("input[placeholder*='email'], input[placeholder*='Discord'], input[placeholder*='username']").first
            await search_input.click()
            await search_input.fill("")
            await search_input.type(email, delay=30)
            print(f"[EngineX] Typed email into search field: {email}")

            await asyncio.sleep(1.5)

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
            print(f"[EngineX] DEBUG dropdown area HTML:\n{debug_dropdown_html}")

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

            search_field_state = await page.evaluate("""
                () => {
                    const input = document.querySelector("input[placeholder*='email'], input[placeholder*='Discord'], input[placeholder*='username']");
                    return input ? input.value : "field_gone";
                }
            """)
            print(f"[EngineX] Search field state after selection: '{search_field_state}'")

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
                return False, "Clicked Grant Access but modal is still open — action likely did not register."
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


async def winsight_revoke(discord_id: str, model_name: str) -> tuple[bool, str]:
    print(f"[Winsight Revoke] Starting revoke for discord_id={discord_id}, model={model_name}")
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

            try:
                title_el = page.locator(f"*:has-text('{model_name}')").last
                model_card = title_el
                chip_locator = None
                count = 0

                for _ in range(8):
                    chip_locator = model_card.locator(
                        f"[data-testid*='badge-share'][data-testid*='{discord_id}']"
                    )
                    count = await chip_locator.count()
                    if count > 0:
                        break
                    model_card = model_card.locator("xpath=..")

                print(f"[Winsight Revoke] Found {count} matching chip(s) for discord_id={discord_id}")

                if count == 0:
                    await browser.close()
                    return False, f"⚠️ {discord_id} doesn't appear to have access to **{model_name}** on Winsight."

                chip_locator = chip_locator.first
                await chip_locator.click(timeout=5000)
                result = "clicked"
            except Exception as e:
                print(f"[Winsight Revoke] Click failed: {e}")
                result = "click_failed"

            await asyncio.sleep(2)
            await browser.close()

            if result == "clicked":
                return True, f"✅ Access revoked for {discord_id} on **{model_name}** (Winsight)."
            else:
                return False, f"❌ Found the chip but couldn't click the remove button (result: {result})."

    except Exception as e:
        print(f"[Winsight Revoke] EXCEPTION: {str(e)}")
        return False, f"Error revoking on Winsight: {str(e)}"


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
#  COMMANDES MANUELLES — avec autocomplete
# ─────────────────────────────────────────────

async def model_autocomplete(interaction: discord.Interaction, current: str):
    products = load_products()
    matches = [name for name in products.keys() if current.lower() in name.lower()]
    return [app_commands.Choice(name=name, value=name) for name in matches[:25]]


async def pipeline_autocomplete(interaction: discord.Interaction, current: str):
    pipelines = load_pipelines()
    matches = [name for name in pipelines.keys() if current.lower() in name.lower()]
    return [app_commands.Choice(name=name, value=name) for name in matches[:25]]


def get_platforms_for_model(model_name: str) -> list:
    products = load_products()
    return list(products.get(model_name, {}).keys())


@tree.command(name="setpipeline", description="Enregistrer le nom exact d'une pipeline sur Winsight")
@app_commands.describe(
    pipeline="Nom court à utiliser dans Discord",
    site_name="Nom exact de la pipeline tel qu'il apparaît sur Winsight",
)
@app_commands.checks.has_permissions(administrator=True)
async def setpipeline_cmd(interaction: discord.Interaction, pipeline: str, site_name: str):
    pipelines = load_pipelines()
    pipelines[pipeline] = site_name
    save_pipelines(pipelines)
    await interaction.response.send_message(
        f"✅ Pipeline **{pipeline}** enregistrée avec le nom Winsight exact: `{site_name}`.",
        ephemeral=True,
    )


@tree.command(name="removepipeline", description="Supprimer une pipeline enregistrée")
@app_commands.describe(pipeline="Pipeline à supprimer")
@app_commands.autocomplete(pipeline=pipeline_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def removepipeline_cmd(interaction: discord.Interaction, pipeline: str):
    pipelines = load_pipelines()
    if pipeline not in pipelines:
        await interaction.response.send_message(f"⚠️ Pipeline **{pipeline}** introuvable.", ephemeral=True)
        return

    del pipelines[pipeline]
    save_pipelines(pipelines)
    await interaction.response.send_message(f"🚫 Pipeline **{pipeline}** supprimée.", ephemeral=True)


@tree.command(name="pipelinelist", description="Afficher les pipelines enregistrées")
@app_commands.checks.has_permissions(administrator=True)
async def pipelinelist_cmd(interaction: discord.Interaction):
    pipelines = load_pipelines()
    if not pipelines:
        await interaction.response.send_message("Aucune pipeline enregistrée.", ephemeral=True)
        return

    lines = [f"● **{name}** → `{site_name}`" for name, site_name in pipelines.items()]
    embed = discord.Embed(
        title="Pipelines Winsight enregistrées",
        description="\n".join(lines),
        color=EMBED_COLOR,
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="pipelineadd", description="Ajouter une pipeline Winsight à un utilisateur")
@app_commands.describe(
    pipeline="Pipeline enregistrée à ajouter",
    discord_id="ID Discord du client sur Winsight",
)
@app_commands.autocomplete(pipeline=pipeline_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def pipelineadd_cmd(interaction: discord.Interaction, pipeline: str, discord_id: str):
    pipelines = load_pipelines()
    if pipeline not in pipelines:
        await interaction.response.send_message(
            f"❌ Pipeline **{pipeline}** introuvable. Utilise d'abord `/setpipeline` avec le nom exact Winsight.",
            ephemeral=True,
        )
        return

    site_name = pipelines[pipeline]
    await interaction.response.send_message(
        f"⏳ Adding pipeline **{pipeline}** (`{site_name}` on Winsight) to `{discord_id}`...",
        ephemeral=True,
    )

    async def run_pipeline_add():
        success, message = await winsight_grant_pipeline(discord_id, site_name)
        embed = discord.Embed(
            title=f"✅ Pipeline Added — {pipeline}" if success else "❌ Pipeline Add Failed",
            description=message,
            color=0x57F287 if success else 0xED4245,
        )
        embed.add_field(name="User", value=f"<@{discord_id}> ({discord_id})", inline=False)
        embed.add_field(name="Winsight Name", value=site_name, inline=False)
        embed.set_footer(text=f"Pipeline Add • by {interaction.user}")
        await interaction.followup.send(embed=embed, ephemeral=True)

        if STAFF_CHANNEL_ID:
            channel = bot.get_channel(STAFF_CHANNEL_ID)
            if channel:
                staff_embed = discord.Embed(
                    title=f"✅ Pipeline Added — {pipeline}" if success else "❌ Pipeline Add Failed",
                    description=message,
                    color=0x57F287 if success else 0xED4245,
                )
                staff_embed.add_field(name="User", value=f"<@{discord_id}> ({discord_id})", inline=False)
                staff_embed.add_field(name="Winsight Name", value=site_name, inline=False)
                staff_embed.set_footer(text=f"Pipeline Add by {interaction.user}")
                await channel.send(embed=staff_embed)

    asyncio.create_task(run_pipeline_add())


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

        if success:
            # Titre style capture : "✅ WIN Granted — Model" ou "✅ EX Granted — Model"
            if platform.value == "winsight":
                title = f"✅ WIN Granted — {model}"
            else:
                title = f"✅ EX Granted — {model}"

            embed = discord.Embed(title=title, color=0x57F287)
            embed.description = f"✓ {model}"

            if platform.value == "winsight":
                embed.add_field(name="User", value=f"<@{contact}> ({contact})", inline=False)
            else:
                # Pour EngineX on n'a pas de Discord ID, on affiche l'email
                embed.add_field(name="Discord User", value=f"N/A (manual grant)", inline=False)
                embed.add_field(name="EngineX Email", value=contact, inline=False)

            embed.add_field(name="Result", value="1 granted", inline=False)
            embed.set_footer(text=f"Manual Grant • by {interaction.user}")
        else:
            title = "❌ Manual Grant Failed"
            embed = discord.Embed(
                title=title,
                description=message,
                color=0xED4245,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

        # Poster aussi dans le salon staff si configuré
        if STAFF_CHANNEL_ID:
            channel = bot.get_channel(STAFF_CHANNEL_ID)
            if channel:
                if success:
                    staff_embed = discord.Embed(title=title, color=0x57F287)
                    staff_embed.description = f"✓ {model}"
                    if platform.value == "winsight":
                        staff_embed.add_field(name="User", value=f"<@{contact}> ({contact})", inline=False)
                    else:
                        staff_embed.add_field(name="Discord User", value=f"N/A (manual grant)", inline=False)
                        staff_embed.add_field(name="EngineX Email", value=contact, inline=False)
                    staff_embed.add_field(name="Result", value="1 granted — Manual", inline=False)
                    staff_embed.set_footer(text=f"Granted by {interaction.user} • Manual")
                else:
                    staff_embed = discord.Embed(
                        title="❌ Manual Grant Failed",
                        description=message,
                        color=0xED4245,
                    )
                    staff_embed.set_footer(text=f"Attempted by {interaction.user}")
                await channel.send(embed=staff_embed)

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


@tree.command(name="revoke", description="Retirer l'accès d'un client à un modèle (Winsight uniquement pour l'instant)")
@app_commands.describe(
    model="Le modèle à retirer",
    contact="ID Discord du client (Winsight uniquement)",
)
@app_commands.autocomplete(model=model_autocomplete)
@app_commands.checks.has_permissions(administrator=True)
async def revoke_cmd(interaction: discord.Interaction, model: str, contact: str):
    available = get_platforms_for_model(model)
    if "winsight" not in available:
        await interaction.response.send_message(
            f"❌ **{model}** is not configured on Winsight. /revoke currently only supports Winsight.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"⏳ Revoking access to **{model}** on **Winsight** for `{contact}`...", ephemeral=True
    )

    async def run_revoke():
        success, message = await winsight_revoke(contact, model)
        embed = discord.Embed(
            title="✅ Access Revoked" if success else "❌ Revoke Failed",
            description=message,
            color=0x57F287 if success else 0xED4245,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    asyncio.create_task(run_revoke())


# ─────────────────────────────────────────────
#  COMMANDE /vouch — témoignages clients
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
            await interaction.response.send_message(
                "⚠️ Vouch channel is not configured. Please contact an admin.",
                ephemeral=True,
            )
            return

        channel = bot.get_channel(VOUCH_CHANNEL_ID)
        if not channel:
            await interaction.response.send_message(
                "⚠️ Could not find the vouch channel. Please contact an admin.",
                ephemeral=True,
            )
            return

        stars = "⭐" * self.rating + "☆" * (5 - self.rating)

        embed = discord.Embed(
            title=f"{stars}",
            description=str(self.text_input.value),
            color=0xF1C40F,
        )
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
        rating = int(self.values[0])
        await interaction.response.send_modal(VouchModal(rating))


@tree.command(name="vouch", description="Laisser un témoignage sur votre expérience")
async def vouch_cmd(interaction: discord.Interaction):
    view = discord.ui.View(timeout=120)
    view.add_item(RatingSelect())
    await interaction.response.send_message("How would you rate your experience?", view=view, ephemeral=True)


# ─────────────────────────────────────────────
#  LANCEMENT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    main_loop.run_until_complete(bot.start(BOT_TOKEN))
