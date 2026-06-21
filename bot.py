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

# Produits disponibles : nom du modèle -> {"price": centimes USD, "platform": "winsight" | "enginex"}
DEFAULT_PRODUCTS = {
    "XyCubValorantV2": {"price": 2000, "platform": "winsight"},
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
    # Rétrocompatibilité : si un produit est juste un nombre (ancien format), on le convertit
    normalized = {}
    for name, value in data.items():
        if isinstance(value, dict):
            normalized[name] = value
        else:
            normalized[name] = {"price": value, "platform": "winsight"}
    return normalized


def create_order(order_id: str, buyer_id: int, buyer_contact: str, model: str, stripe_session_id: str):
    data = load_orders()
    data[order_id] = {
        "buyer_id": buyer_id,
        "buyer_contact": buyer_contact,
        "model": model,
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
    def __init__(self, model_name: str):
        products = load_products()
        platform = products.get(model_name, {}).get("platform", "winsight")

        if platform == "enginex":
            label = "Your email (used on EngineX)"
        else:
            label = "Your Discord username or ID (used on Winsight)"

        super().__init__(title="Order Details")
        self.model_name = model_name
        self.contact_input = discord.ui.TextInput(label=label, max_length=200, required=True)
        self.add_item(self.contact_input)

    async def on_submit(self, interaction: discord.Interaction):
        products = load_products()

        if self.model_name not in products:
            await interaction.response.send_message(
                f"❌ Model '{self.model_name}' is no longer available. Please start over.",
                ephemeral=True,
            )
            return

        price_cents = products[self.model_name]["price"]
        order_id = f"{interaction.user.id}_{int(time.time() * 1000)}"

        try:
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": f"Weight: {self.model_name}"},
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
            stripe_session_id=checkout_session.id,
        )

        embed = discord.Embed(
            title="💳 Complete Your Payment",
            description=(
                f"**Model:** {self.model_name}\n**Price:** ${price_cents / 100:.2f}\n\n"
                f"Click below to pay securely via Stripe. "
                f"Once paid, your weight will be delivered automatically — no further action needed!"
            ),
            color=0xF1C40F,
        )
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Pay Now", url=checkout_session.url, style=discord.ButtonStyle.link))

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class ModelSelect(discord.ui.Select):
    def __init__(self):
        products = load_products()
        options = [
            discord.SelectOption(
                label=name,
                description=f"${info['price'] / 100:.2f} • {info['platform'].capitalize()}",
            )
            for name, info in products.items()
        ][:25]

        if not options:
            options = [discord.SelectOption(label="No products available", value="__none__")]

        super().__init__(placeholder="Choose a model...", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "__none__":
            await interaction.response.send_message("❌ No products are currently available.", ephemeral=True)
            return
        await interaction.response.send_modal(ContactOnlyModal(self.values[0]))


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


@tree.command(name="addproduct", description="Ajouter ou mettre à jour un modèle en vente")
@app_commands.choices(platform=platform_choices)
@app_commands.checks.has_permissions(administrator=True)
async def addproduct_cmd(interaction: discord.Interaction, model: str, price_usd: float, platform: app_commands.Choice[str]):
    products = load_products()
    products[model] = {"price": int(price_usd * 100), "platform": platform.value}
    save_json(PRODUCTS_FILE, products)
    await interaction.response.send_message(
        f"✅ Product **{model}** set to ${price_usd:.2f} on **{platform.name}**.", ephemeral=True
    )


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

            # Cliquer sur le résultat de recherche qui apparaît (contient l'email tapé)
            result_clicked = await page.evaluate(f"""
                () => {{
                    const emailLower = "{email}".toLowerCase();
                    const allElements = document.querySelectorAll("*");
                    for (const el of allElements) {{
                        let directText = "";
                        for (const node of el.childNodes) {{
                            if (node.nodeType === Node.TEXT_NODE) {{
                                directText += node.textContent;
                            }}
                        }}
                        if (directText.toLowerCase().includes(emailLower)) {{
                            // Remonte jusqu'à trouver un élément cliquable (souvent le parent direct du résultat)
                            let clickable = el;
                            for (let i = 0; i < 5; i++) {{
                                if (clickable.onclick || clickable.getAttribute("role") === "button" || clickable.tagName === "BUTTON" || clickable.tagName === "LI" || clickable.tagName === "DIV") {{
                                    clickable.click();
                                    return true;
                                }}
                                clickable = clickable.parentElement;
                                if (!clickable) break;
                            }}
                        }}
                    }}
                    return false;
                }}
            """)
            print(f"[EngineX] Search result clicked: {result_clicked}")

            if not result_clicked:
                await browser.close()
                return False, f"Could not find user matching email '{email}' in search results."

            await asyncio.sleep(0.5)

            # Sélectionner le bon modèle dans le dropdown
            model_selected = await page.evaluate(f"""
                () => {{
                    const modelName = "{model_name}".toLowerCase();
                    const selects = document.querySelectorAll("select");
                    for (const select of selects) {{
                        for (const option of select.options) {{
                            if (option.textContent.toLowerCase().includes(modelName)) {{
                                select.value = option.value;
                                select.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                return true;
                            }}
                        }}
                    }}
                    return false;
                }}
            """)
            print(f"[EngineX] Model '{model_name}' selected in dropdown: {model_selected}")

            if not model_selected:
                await browser.close()
                return False, f"Could not find model '{model_name}' in the dropdown."

            await asyncio.sleep(0.5)

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
            await browser.close()
            print("[EngineX] Browser closed.")

            if final_clicked:
                return True, f"Access granted to {email} for {model_name} on EngineX."
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

    products = load_products()
    platform = products.get(order["model"], {}).get("platform", "winsight")
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
#  LANCEMENT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    main_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(main_loop)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    main_loop.run_until_complete(bot.start(BOT_TOKEN))
