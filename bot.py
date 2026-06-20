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

STAFF_CHANNEL_ID = int(os.environ.get("STAFF_CHANNEL_ID", "0")) or None
ORDER_PANEL_CHANNEL_ID = int(os.environ.get("ORDER_PANEL_CHANNEL_ID", "0")) or None

DATA_DIR = os.environ.get("DATA_DIR", "/data")
os.makedirs(DATA_DIR, exist_ok=True)
ORDERS_FILE = os.path.join(DATA_DIR, "winsight_orders.json")
PRODUCTS_FILE = os.path.join(DATA_DIR, "winsight_products.json")

EMBED_COLOR = 0x2F3136

# Produits disponibles : nom du modèle -> prix en centimes (USD)
DEFAULT_PRODUCTS = {
    "XyCubValorantV2": 2000,  # $20.00
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
    return load_json(PRODUCTS_FILE, DEFAULT_PRODUCTS)


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
        model_name = str(self.model_input.value).strip()
        products = load_products()

        if model_name not in products:
            available = ", ".join(products.keys())
            await interaction.response.send_message(
                f"❌ Unknown model '{model_name}'. Available models: {available}",
                ephemeral=True,
            )
            return

        price_cents = products[model_name]
        order_id = f"{interaction.user.id}_{int(time.time() * 1000)}"

        try:
            checkout_session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": f"Weight: {model_name}"},
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
            model=model_name,
            stripe_session_id=checkout_session.id,
        )

        embed = discord.Embed(
            title="💳 Complete Your Payment",
            description=(
                f"**Model:** {model_name}\n**Price:** ${price_cents / 100:.2f}\n\n"
                f"Click below to pay securely via Stripe. "
                f"Once paid, your weight will be delivered automatically — no further action needed!"
            ),
            color=0xF1C40F,
        )
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label="Pay Now", url=checkout_session.url, style=discord.ButtonStyle.link))

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class OrderStartView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🛒 Order Now", style=discord.ButtonStyle.success, custom_id="winsight_order_start")
    async def order_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(OrderDetailsModal())


@bot.event
async def on_ready():
    global main_loop
    main_loop = asyncio.get_event_loop()
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


@tree.command(name="addproduct", description="Ajouter ou mettre à jour un modèle en vente")
@app_commands.checks.has_permissions(administrator=True)
async def addproduct_cmd(interaction: discord.Interaction, model: str, price_usd: float):
    products = load_products()
    products[model] = int(price_usd * 100)
    save_json(PRODUCTS_FILE, products)
    await interaction.response.send_message(
        f"✅ Product **{model}** set to ${price_usd:.2f}.", ephemeral=True
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
                await page.click("button[type='submit']")
                await page.wait_for_load_state("networkidle", timeout=30000)
                print(f"[Winsight] Logged in, new title: {await page.title()}")
            else:
                print("[Winsight] No login form found (already logged in?)")

            print(f"[Winsight] Searching for model '{model_name}' on page...")
            found = await page.evaluate(f"""
                () => {{
                    const modelName = "{model_name}".toLowerCase();
                    const allElements = document.querySelectorAll("*");

                    for (const el of allElements) {{
                        if (el.children.length === 0 && el.textContent.toLowerCase().includes(modelName)) {{
                            let parent = el;
                            for (let i = 0; i < 10; i++) {{
                                parent = parent.parentElement;
                                if (!parent) break;
                                const input = parent.querySelector("input[placeholder*='username'], input[placeholder*='customer'], input[placeholder*='Username']");
                                if (input) {{
                                    input.value = "{discord_id}";
                                    input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                    input.dispatchEvent(new Event('change', {{ bubbles: true }}));

                                    const buttons = parent.querySelectorAll("button");
                                    for (const btn of buttons) {{
                                        if (btn.textContent.toUpperCase().includes("SHARE")) {{
                                            btn.click();
                                            return "clicked";
                                        }}
                                    }}
                                    return "input_filled_no_button";
                                }}
                            }}
                        }}
                    }}
                    return "not_found";
                }}
            """)
            print(f"[Winsight] Search result: {found}")

            await asyncio.sleep(2)
            await browser.close()
            print("[Winsight] Browser closed.")

            if found == "clicked":
                return True, f"Access granted to {discord_id} for {model_name} on Winsight."
            elif found == "input_filled_no_button":
                return False, "Input filled but Share button not found."
            else:
                return False, f"Could not find model '{model_name}' on Winsight."

    except Exception as e:
        print(f"[Winsight] EXCEPTION: {str(e)}")
        return False, f"Error: {str(e)}"


async def process_paid_order(order_id: str):
    print(f"[Order] Processing paid order: {order_id}")
    order = get_order(order_id)
    if not order:
        print(f"❌ Order {order_id} not found.")
        return

    print(f"[Order] Order data: {order}")
    update_order(order_id, status="processing")

    success, message = await winsight_grant(str(order["buyer_id"]), order["model"])
    print(f"[Order] winsight_grant result: success={success}, message={message}")

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
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(BOT_TOKEN)
