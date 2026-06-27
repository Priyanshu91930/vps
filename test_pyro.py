from pyrogram import Client

# Use the exact same bot credentials
app = Client(
    "test_pyro_session",
    api_id=27686895,
    api_hash="0e996bd3891969ec5dfebf8bb3e39e94",
    bot_token="8615130694:AAF5Y29rp3_pmtHj5dgqS4picI03Kx6Uvxo"
)

@app.on_message()
async def handle_message(client, message):
    print(f"📥 RECEIVED: {message.text} | From User ID: {message.from_user.id if message.from_user else 'Unknown'}")
    await message.reply_text("🤖 Echo: working!")

print("🚀 Test bot is running... Send a message to it now.")
app.run()
