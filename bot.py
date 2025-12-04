import os
import asyncio
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from telethon import TelegramClient, events
from telethon.sessions import StringSession
import requests
import json
from threading import Thread
import uuid

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
API_ID = os.getenv('TELEGRAM_API_ID', 'YOUR_API_ID')
API_HASH = os.getenv('TELEGRAM_API_HASH', 'YOUR_API_HASH')
SESSION_STRING = os.getenv('TELEGRAM_SESSION_STRING', 'YOUR_SESSION_STRING')
ZINIPAY_API_KEY = os.getenv('ZINIPAY_API_KEY', 'YOUR_ZINIPAY_API_KEY')
WEBHOOK_URL = os.getenv('WEBHOOK_URL', 'https://yourdomain.com/webhook')
SUCCESS_URL = os.getenv('SUCCESS_URL', 'https://yourdomain.com/success')
CANCEL_URL = os.getenv('CANCEL_URL', 'https://yourdomain.com/cancel')
FLASK_PORT = int(os.getenv('FLASK_PORT', '5000'))

# Initialize Flask app
app = Flask(__name__)

# Store pending payments: {invoice_id: {user_id, chat_id, amount, created_at}}
pending_payments = {}

# Initialize Telethon client
client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)


def create_zinipay_payment(amount, user_id, metadata=None):
    """Create a payment request with ZiniPay"""
    try:
        invoice_id = str(uuid.uuid4())
        
        payload = {
            "amount": str(amount),
            "redirect_url": f"{SUCCESS_URL}?invoiceId={invoice_id}",
            "cancel_url": f"{CANCEL_URL}?invoiceId={invoice_id}",
            "webhook_url": f"{WEBHOOK_URL}?invoiceId={invoice_id}",
            "metadata": metadata or {"user_id": str(user_id)}
        }
        
        headers = {
            'zini-api-key': ZINIPAY_API_KEY,
            'Content-Type': 'application/json'
        }
        
        response = requests.post(
            'https://api.zinipay.com/v1/payment/create',
            headers=headers,
            json=payload,
            timeout=30
        )
        
        if response.status_code == 200:
            data = response.json()
            if data.get('status'):
                return {
                    'success': True,
                    'payment_url': data.get('payment_url'),
                    'invoice_id': invoice_id
                }
        
        logger.error(f"ZiniPay API error: {response.text}")
        return {'success': False, 'error': 'Failed to create payment'}
        
    except Exception as e:
        logger.error(f"Error creating payment: {e}")
        return {'success': False, 'error': str(e)}


def verify_zinipay_payment(invoice_id):
    """Verify payment status with ZiniPay"""
    try:
        headers = {
            'zini-api-key': ZINIPAY_API_KEY,
            'Content-Type': 'application/json'
        }
        
        payload = {
            'invoiceId': invoice_id,
            'apiKey': ZINIPAY_API_KEY
        }
        
        response = requests.post(
            'https://api.zinipay.com/v1/payment/verify',
            headers=headers,
            json=payload,
            timeout=30
        )
        
        if response.status_code == 200:
            return response.json()
        
        return None
        
    except Exception as e:
        logger.error(f"Error verifying payment: {e}")
        return None


# Flask routes
@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle ZiniPay webhook notifications"""
    try:
        data = request.get_json()
        invoice_id = request.args.get('invoiceId')
        
        logger.info(f"Webhook received for invoice: {invoice_id}")
        logger.info(f"Webhook data: {json.dumps(data, indent=2)}")
        
        if invoice_id and invoice_id in pending_payments:
            payment_info = pending_payments[invoice_id]
            
            # Verify the payment
            verification = verify_zinipay_payment(invoice_id)
            
            if verification and verification.get('status') == 'COMPLETED':
                # Payment successful
                asyncio.run_coroutine_threadsafe(
                    notify_payment_success(payment_info, verification),
                    client.loop
                )
                
                # Remove from pending
                del pending_payments[invoice_id]
            
        return jsonify({'status': 'ok'}), 200
        
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/success', methods=['GET'])
def success():
    """Handle successful payment redirect"""
    invoice_id = request.args.get('invoiceId')
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Payment Successful</title>
        <style>
            body {{
                font-family: Arial, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            }}
            .container {{
                background: white;
                padding: 40px;
                border-radius: 10px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                text-align: center;
            }}
            .success-icon {{
                font-size: 60px;
                color: #4CAF50;
                margin-bottom: 20px;
            }}
            h1 {{
                color: #333;
                margin-bottom: 10px;
            }}
            p {{
                color: #666;
                font-size: 16px;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="success-icon">✓</div>
            <h1>Payment Successful!</h1>
            <p>Your payment has been processed successfully.</p>
            <p>You will receive a confirmation on Telegram shortly.</p>
            <p style="margin-top: 20px; font-size: 14px; color: #999;">
                Invoice ID: {invoice_id}
            </p>
        </div>
    </body>
    </html>
    """
    return html


@app.route('/cancel', methods=['GET'])
def cancel():
    """Handle cancelled payment redirect"""
    invoice_id = request.args.get('invoiceId')
    
    if invoice_id and invoice_id in pending_payments:
        payment_info = pending_payments[invoice_id]
        asyncio.run_coroutine_threadsafe(
            notify_payment_cancelled(payment_info),
            client.loop
        )
        del pending_payments[invoice_id]
    
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Payment Cancelled</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                display: flex;
                justify-content: center;
                align-items: center;
                height: 100vh;
                margin: 0;
                background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
            }
            .container {
                background: white;
                padding: 40px;
                border-radius: 10px;
                box-shadow: 0 10px 40px rgba(0,0,0,0.2);
                text-align: center;
            }
            .cancel-icon {
                font-size: 60px;
                color: #f44336;
                margin-bottom: 20px;
            }
            h1 {
                color: #333;
                margin-bottom: 10px;
            }
            p {
                color: #666;
                font-size: 16px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <div class="cancel-icon">✕</div>
            <h1>Payment Cancelled</h1>
            <p>Your payment was cancelled.</p>
            <p>No charges have been made.</p>
        </div>
    </body>
    </html>
    """
    return html


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'ok',
        'pending_payments': len(pending_payments),
        'timestamp': datetime.now().isoformat()
    })


# Telethon event handlers
async def notify_payment_success(payment_info, verification):
    """Send payment success notification to user"""
    try:
        chat_id = payment_info['chat_id']
        amount = verification.get('amount', payment_info['amount'])
        transaction_id = verification.get('transactionId', 'N/A')
        payment_method = verification.get('paymentMethod', 'N/A')
        
        message = f"""
✅ **Payment Successful!**

💰 **Amount:** {amount} BDT
🔖 **Transaction ID:** {transaction_id}
💳 **Payment Method:** {payment_method.upper()}
⏰ **Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Thank you for your payment!
"""
        
        await client.send_message(chat_id, message)
        logger.info(f"Payment success notification sent to {chat_id}")
        
    except Exception as e:
        logger.error(f"Error sending success notification: {e}")


async def notify_payment_cancelled(payment_info):
    """Send payment cancellation notification to user"""
    try:
        chat_id = payment_info['chat_id']
        amount = payment_info['amount']
        
        message = f"""
❌ **Payment Cancelled**

💰 **Amount:** {amount} BDT

Your payment was cancelled. No charges have been made.
If you want to retry, send `.pay {amount}` again.
"""
        
        await client.send_message(chat_id, message)
        logger.info(f"Payment cancelled notification sent to {chat_id}")
        
    except Exception as e:
        logger.error(f"Error sending cancellation notification: {e}")


@client.on(events.NewMessage(pattern=r'\.pay\s+(\d+(?:\.\d{1,2})?)'))
async def handle_pay_command(event):
    """Handle .pay command"""
    try:
        # Extract amount from command
        amount = float(event.pattern_match.group(1))
        
        if amount <= 0:
            await event.reply("❌ Amount must be greater than 0")
            return
        
        # Get user info
        sender = await event.get_sender()
        user_id = sender.id
        chat_id = event.chat_id
        
        # Send processing message
        processing_msg = await event.reply("⏳ Creating payment link...")
        
        # Create payment with ZiniPay
        result = create_zinipay_payment(
            amount=amount,
            user_id=user_id,
            metadata={
                "user_id": str(user_id),
                "username": sender.username or "N/A",
                "first_name": sender.first_name or "N/A"
            }
        )
        
        if result['success']:
            invoice_id = result['invoice_id']
            payment_url = result['payment_url']
            
            # Store pending payment
            pending_payments[invoice_id] = {
                'user_id': user_id,
                'chat_id': chat_id,
                'amount': amount,
                'created_at': datetime.now().isoformat()
            }
            
            # Send payment link
            message = f"""
💳 **Payment Link Generated**

💰 **Amount:** {amount} BDT
🔗 **Payment Link:** {payment_url}

📱 **Accepted Methods:**
• bKash
• Nagad
• Rocket

⏱ Click the link to complete your payment.
You will receive a confirmation once the payment is successful.
"""
            
            await processing_msg.edit(message)
            logger.info(f"Payment link created for user {user_id}: {invoice_id}")
            
        else:
            error_msg = result.get('error', 'Unknown error')
            await processing_msg.edit(f"❌ Failed to create payment link: {error_msg}")
            logger.error(f"Payment creation failed: {error_msg}")
        
    except ValueError:
        await event.reply("❌ Invalid amount. Please use format: `.pay 150`")
    except Exception as e:
        logger.error(f"Error handling pay command: {e}")
        await event.reply(f"❌ An error occurred: {str(e)}")


@client.on(events.NewMessage(pattern=r'\.payments'))
async def handle_payments_command(event):
    """Handle .payments command to show pending payments"""
    try:
        if not pending_payments:
            await event.reply("📭 No pending payments")
            return
        
        message = "📋 **Pending Payments:**\n\n"
        
        for invoice_id, info in pending_payments.items():
            message += f"💰 Amount: {info['amount']} BDT\n"
            message += f"🆔 Invoice: {invoice_id[:20]}...\n"
            message += f"⏰ Created: {info['created_at']}\n"
            message += "─" * 30 + "\n\n"
        
        await event.reply(message)
        
    except Exception as e:
        logger.error(f"Error handling payments command: {e}")
        await event.reply(f"❌ An error occurred: {str(e)}")


@client.on(events.NewMessage(pattern=r'\.help'))
async def handle_help_command(event):
    """Handle .help command"""
    help_text = """
🤖 **ZiniPay Payment Bot Commands**

📝 **Available Commands:**

`.pay <amount>` - Create a payment link
   Example: `.pay 150`

`.payments` - View pending payments

`.help` - Show this help message

💡 **How it works:**
1. Send `.pay` command with amount
2. Click the generated payment link
3. Complete payment via bKash/Nagad/Rocket
4. Receive automatic confirmation

🔒 Secure payments powered by ZiniPay
"""
    await event.reply(help_text)


def run_flask():
    """Run Flask app in a separate thread"""
    app.run(host='0.0.0.0', port=FLASK_PORT, debug=False)


async def main():
    """Main function to start the bot"""
    logger.info("Starting ZiniPay Telegram Userbot...")
    
    # Start Flask in a separate thread
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask server started on port {FLASK_PORT}")
    
    # Connect Telethon client
    await client.start()
    logger.info("Telethon client connected")
    
    me = await client.get_me()
    logger.info(f"Logged in as: {me.first_name} (@{me.username})")
    
    logger.info("Bot is ready! Send .help for commands")
    
    # Keep the bot running
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        client.loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        client.disconnect()
