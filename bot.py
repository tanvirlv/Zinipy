import os
import asyncio
import logging
from threading import Thread
from datetime import datetime
from dotenv import load_dotenv
from flask import Flask, request, jsonify
from telethon import TelegramClient, events
import requests
import hmac
import hashlib

# Load environment variables
load_dotenv()

# Configuration
API_ID = int(os.getenv('API_ID'))
API_HASH = os.getenv('API_HASH')
SESSION_STRING = os.getenv('SESSION_STRING')
ZINIPAY_API_KEY = os.getenv('ZINIPAY_API_KEY')
ZINIPAY_SECRET = os.getenv('ZINIPAY_SECRET')

# Render automatically provides PORT, fallback to 5000 for local
PORT = int(os.getenv('PORT', 5000))
HOST = '0.0.0.0'

# Auto-detect webhook URL from Render
RENDER_EXTERNAL_URL = os.getenv('RENDER_EXTERNAL_URL')
if RENDER_EXTERNAL_URL:
    WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}/webhook"
else:
    # Fallback for local development
    WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'http://localhost:5000/webhook')

# Logging setup
logging.basicConfig(
    format='[%(levelname)s/%(asctime)s] %(name)s: %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)

# Payment storage (consider using Render's PostgreSQL or Redis add-on for production)
payments = {}

# Telethon client - use StringSession
from telethon.sessions import StringSession

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH
)


class ZiniPayAPI:
    """ZiniPay API Integration"""
    
    BASE_URL = "https://api.zinipay.com/v1"
    
    def __init__(self, api_key, secret):
        self.api_key = api_key
        self.secret = secret
        self.headers = {
            'Authorization': f'Bearer {api_key}',
            'Content-Type': 'application/json'
        }
    
    def create_payment(self, amount, currency='USD', description='Payment'):
        """Create a payment link"""
        try:
            payload = {
                'amount': amount,
                'currency': currency,
                'description': description,
                'webhook_url': WEBHOOK_URL,
                'return_url': 'https://t.me/',
            }
            
            logger.info(f"Creating payment with webhook: {WEBHOOK_URL}")
            
            response = requests.post(
                f'{self.BASE_URL}/payments',
                headers=self.headers,
                json=payload,
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"ZiniPay API Error: {e}")
            return None
    
    def verify_webhook_signature(self, payload, signature):
        """Verify webhook signature"""
        expected_signature = hmac.new(
            self.secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected_signature, signature)


# Initialize ZiniPay
zinipay = ZiniPayAPI(ZINIPAY_API_KEY, ZINIPAY_SECRET)

# Global event loop reference
loop = None


# Flask Routes
@app.route('/')
def index():
    """Root endpoint"""
    return jsonify({
        'status': 'online',
        'service': 'Telegram Payment Bot',
        'webhook_url': WEBHOOK_URL
    }), 200


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint for Render"""
    return jsonify({
        'status': 'healthy',
        'active_payments': len(payments),
        'webhook_configured': WEBHOOK_URL
    }), 200


@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle ZiniPay webhooks"""
    try:
        # Get signature from header
        signature = request.headers.get('X-ZiniPay-Signature', '')
        payload = request.get_data(as_text=True)
        
        # Verify signature
        if not zinipay.verify_webhook_signature(payload, signature):
            logger.warning("Invalid webhook signature")
            return jsonify({'error': 'Invalid signature'}), 401
        
        # Parse webhook data
        data = request.get_json()
        payment_id = data.get('payment_id')
        status = data.get('status')
        
        logger.info(f"Webhook received: {payment_id} - {status}")
        
        # Check if payment exists in our system
        if payment_id not in payments:
            logger.warning(f"Unknown payment ID: {payment_id}")
            return jsonify({'error': 'Unknown payment'}), 404
        
        payment_info = payments[payment_id]
        
        # Handle payment status
        if status == 'completed' or status == 'success':
            # Mark as paid
            payment_info['status'] = 'paid'
            payment_info['paid_at'] = datetime.now().isoformat()
            
            # Notify user via Telegram
            if loop:
                asyncio.run_coroutine_threadsafe(
                    notify_payment_success(payment_info),
                    loop
                )
        elif status == 'failed':
            payment_info['status'] = 'failed'
            if loop:
                asyncio.run_coroutine_threadsafe(
                    notify_payment_failed(payment_info),
                    loop
                )
        
        return jsonify({'status': 'success'}), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'error': str(e)}), 500


# Telethon Event Handlers
@client.on(events.NewMessage(pattern=r'\.pay (\d+(?:\.\d{1,2})?)'))
async def handle_payment_command(event):
    """Handle .pay command"""
    try:
        # Extract amount
        amount = float(event.pattern_match.group(1))
        
        if amount <= 0:
            await event.reply("❌ Amount must be greater than 0")
            return
        
        if amount > 10000:
            await event.reply("❌ Amount too large. Maximum is 10,000")
            return
        
        # Get user info
        sender = await event.get_sender()
        user_id = sender.id
        username = sender.username or sender.first_name or "User"
        
        # Send processing message
        processing_msg = await event.reply(f"⏳ Creating payment link for ${amount:.2f}...")
        
        # Create payment via ZiniPay
        description = f"Payment from @{username} (ID: {user_id})"
        payment_data = zinipay.create_payment(
            amount=amount,
            currency='USD',
            description=description
        )
        
        if not payment_data:
            await processing_msg.edit("❌ Failed to create payment. Please try again later.")
            return
        
        payment_id = payment_data.get('id') or payment_data.get('payment_id')
        payment_url = payment_data.get('payment_url') or payment_data.get('url')
        
        # Store payment info
        payments[payment_id] = {
            'payment_id': payment_id,
            'user_id': user_id,
            'username': username,
            'amount': amount,
            'status': 'pending',
            'created_at': datetime.now().isoformat(),
            'chat_id': event.chat_id,
            'message_id': processing_msg.id
        }
        
        # Send payment link
        message = (
            f"💳 **Payment Link Created**\n\n"
            f"Amount: `${amount:.2f} USD`\n"
            f"Payment ID: `{payment_id}`\n"
            f"Status: Pending\n\n"
            f"👇 Click below to pay:\n"
            f"{payment_url}\n\n"
            f"⏱ Link expires in 24 hours"
        )
        
        await processing_msg.edit(message)
        logger.info(f"Payment created: {payment_id} for user {user_id}")
        
    except ValueError:
        await event.reply("❌ Invalid amount format. Use: `.pay 150` or `.pay 150.50`")
    except Exception as e:
        logger.error(f"Error handling payment command: {e}")
        await event.reply(f"❌ An error occurred: {str(e)}")


@client.on(events.NewMessage(pattern=r'\.checkpay (\S+)'))
async def handle_check_payment(event):
    """Check payment status"""
    try:
        payment_id = event.pattern_match.group(1)
        
        if payment_id not in payments:
            await event.reply("❌ Payment not found")
            return
        
        payment_info = payments[payment_id]
        sender = await event.get_sender()
        
        # Only allow the payment creator to check
        if sender.id != payment_info['user_id']:
            await event.reply("❌ You can only check your own payments")
            return
        
        status = payment_info['status']
        amount = payment_info['amount']
        created_at = payment_info['created_at']
        
        message = (
            f"💳 **Payment Status**\n\n"
            f"Payment ID: `{payment_id}`\n"
            f"Amount: `${amount:.2f} USD`\n"
            f"Status: {status.upper()}\n"
            f"Created: {created_at}"
        )
        
        if status == 'paid':
            paid_at = payment_info.get('paid_at', 'N/A')
            message += f"\nPaid at: {paid_at}"
        
        await event.reply(message)
        
    except Exception as e:
        logger.error(f"Error checking payment: {e}")
        await event.reply(f"❌ Error: {str(e)}")


@client.on(events.NewMessage(pattern=r'\.mypayments'))
async def handle_my_payments(event):
    """List user's payments"""
    try:
        sender = await event.get_sender()
        user_id = sender.id
        
        user_payments = [p for p in payments.values() if p['user_id'] == user_id]
        
        if not user_payments:
            await event.reply("📭 You have no payments yet.")
            return
        
        message = "📊 **Your Payments**\n\n"
        for payment in user_payments[-10:]:  # Last 10 payments
            status_emoji = "✅" if payment['status'] == 'paid' else "⏳" if payment['status'] == 'pending' else "❌"
            message += f"{status_emoji} `${payment['amount']:.2f}` - {payment['status'].upper()}\n"
            message += f"   ID: `{payment['payment_id']}`\n\n"
        
        await event.reply(message)
        
    except Exception as e:
        logger.error(f"Error listing payments: {e}")
        await event.reply(f"❌ Error: {str(e)}")


@client.on(events.NewMessage(pattern=r'\.help'))
async def handle_help(event):
    """Show help message"""
    help_text = """
**💰 Payment Bot Commands**

`.pay <amount>` - Create a payment link
Example: `.pay 150` or `.pay 150.50`

`.checkpay <payment_id>` - Check payment status
Example: `.checkpay abc123xyz`

`.mypayments` - List your recent payments

`.help` - Show this message

**Supported currencies:** USD
**Payment processor:** ZiniPay
    """
    await event.reply(help_text)


# Notification Functions
async def notify_payment_success(payment_info):
    """Notify user of successful payment"""
    try:
        message = (
            f"✅ **Payment Successful!**\n\n"
            f"Payment ID: `{payment_info['payment_id']}`\n"
            f"Amount: `${payment_info['amount']:.2f} USD`\n"
            f"Status: PAID ✅\n"
            f"Thank you for your payment!"
        )
        
        await client.send_message(
            payment_info['chat_id'],
            message,
            reply_to=payment_info['message_id']
        )
        
        logger.info(f"Payment success notification sent: {payment_info['payment_id']}")
    except Exception as e:
        logger.error(f"Error sending success notification: {e}")


async def notify_payment_failed(payment_info):
    """Notify user of failed payment"""
    try:
        message = (
            f"❌ **Payment Failed**\n\n"
            f"Payment ID: `{payment_info['payment_id']}`\n"
            f"Amount: `${payment_info['amount']:.2f} USD`\n"
            f"Status: FAILED\n"
            f"Please try again or contact support."
        )
        
        await client.send_message(
            payment_info['chat_id'],
            message,
            reply_to=payment_info['message_id']
        )
        
        logger.info(f"Payment failed notification sent: {payment_info['payment_id']}")
    except Exception as e:
        logger.error(f"Error sending failed notification: {e}")


# Flask runner in separate thread
def run_flask():
    """Run Flask app"""
    logger.info(f"Starting Flask on {HOST}:{PORT}")
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False)


# Telethon runner
async def run_telethon():
    """Run Telethon client"""
    global loop
    loop = asyncio.get_event_loop()
    
    logger.info("Connecting to Telegram...")
    await client.connect()
    
    if not await client.is_user_authorized():
        logger.error("Session string is invalid or expired!")
        return
    
    me = await client.get_me()
    logger.info(f"Logged in as {me.first_name} (@{me.username})")
    logger.info(f"Webhook URL: {WEBHOOK_URL}")
    logger.info("Bot is ready! Send .help for commands")
    
    await client.run_until_disconnected()


# Main execution
if __name__ == '__main__':
    try:
        logger.info("=" * 50)
        logger.info("Starting Telegram Payment Bot on Render")
        logger.info("=" * 50)
        
        # Start Flask in a separate thread
        flask_thread = Thread(target=run_flask, daemon=True)
        flask_thread.start()
        
        # Give Flask time to start
        import time
        time.sleep(2)
        
        # Run Telethon in the main thread
        asyncio.run(run_telethon())
        
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
