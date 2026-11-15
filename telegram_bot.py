import os, asyncio, traceback
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

import brain
from flow import handle_message, store, human_took_over, give_back_to_bot, review_flagged_order
from messages import T
from events import emit
from security import sec
from notify import staff_chat, set_staff_chat, client_of_alert
import re, datetime as dt
import auditor

load_dotenv()


def client_id_of(update: Update) -> str:
    """The Telegram user's ID, used like a phone number in the Clients tab."""
    return f"telegram:{update.effective_user.id}"


async def on_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = client_id_of(update)
    print("START:", cid, "| name:", update.effective_user.full_name)
    await update.message.reply_text(T("arabizi", "welcome"))
    c = await asyncio.to_thread(store.get_client, cid)
    emit("message_out", client=c["shop_name"] if c else update.effective_user.full_name, text=T("arabizi", "welcome"))


async def on_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Clear this client's conversation (for testing)."""
    cid = client_id_of(update)
    await asyncio.to_thread(store.save_conversation, cid, "IDLE", {})
    print("RESET:", cid)
    await update.message.reply_text("Reset. Ektebli talabiyye jdide.")
    c = await asyncio.to_thread(store.get_client, cid)
    emit("message_out", client=c["shop_name"] if c else cid, text="Reset. Ektebli talabiyye jdide.")


async def on_staff(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Send /staff from the team's chat (a group with the bot, or a teammate's private chat)."""
    current = staff_chat()
    if current and current != str(update.effective_chat.id):
        sec(update.effective_user.full_name, "access", "staff access", "block",
            "/staff from an unregistered chat: refused (staff chat is already set)")
        await update.message.reply_text("Not allowed.")
        return
    set_staff_chat(update.effective_chat.id)
    sec(update.effective_user.full_name, "access", "staff access", "pass", "staff chat registered")
    print("STAFF CHAT SET:", update.effective_chat.id)
    await update.message.reply_text("\u2705 This chat now receives 3al Tayer escalations.\n"
                                    "Reply to an alert to answer the client directly.")


async def on_staff_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """A teammate wrote in the staff chat. If it replies to an alert, forward it to that client."""
    msg = update.message
    target = client_of_alert(msg.reply_to_message.text if msg.reply_to_message else "")
    if not target:
        await msg.reply_text("Reply to an alert (swipe on it) to answer that client.")
        return
    if not target.startswith("telegram:"):
        await msg.reply_text(f"Can't reach {target} from Telegram. Call them instead.")
        return
    client = await asyncio.to_thread(store.get_client, target)
    name = client["shop_name"] if client else target
    await context.bot.send_message(chat_id=int(target.split(":", 1)[1]),
                                   text=f"\U0001f464 {msg.from_user.first_name} (3al Tayer team):\n{msg.text}")
    await asyncio.to_thread(human_took_over, target)         # the bot stays silent for this client
    emit("message_out", client=name, text=f"[HUMAN {msg.from_user.first_name}] {msg.text}")
    print(f"HUMAN -> {target}: {msg.text}")
    await msg.reply_text(f"\u2705 Sent to {name}. Bot paused for them: reply /done to an alert when finished.")


async def on_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Staff replies /done to an alert: the bot takes the conversation back."""
    msg = update.message
    if str(update.effective_chat.id) != staff_chat():
        sec(update.effective_user.full_name, "access", "staff access", "block", "/done from a non-staff chat: ignored")
        return
    target = client_of_alert(msg.reply_to_message.text if msg.reply_to_message else "")
    if not target:
        await msg.reply_text("Reply /done to an alert of the client you finished with.")
        return
    back = await asyncio.to_thread(give_back_to_bot, target)
    client = await asyncio.to_thread(store.get_client, target)
    name = client["shop_name"] if client else target
    if target.startswith("telegram:"):
        await context.bot.send_message(chat_id=int(target.split(":", 1)[1]), text=back)
    emit("message_out", client=name, text=back)
    print(f"BOT RESUMED for {target}")
    await msg.reply_text(f"\U0001f916 Bot is back with {name}.")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_chat.id) == staff_chat():          # the team's chat is not a client
        return await on_staff_message(update, context)
    cid = client_id_of(update)
    text = update.message.text
    print("IN :", cid, "|", text)

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    brain.CALLS["n"] = 0
    try:
        reply = await asyncio.to_thread(handle_message, cid, text, update.effective_user.full_name, True)
    except Exception as e:
        print("ERROR:", repr(e))
        traceback.print_exc()
        reply = "Sorry, sar fi ghalat. Jarreb kamen mara."
        c = await asyncio.to_thread(store.get_client, cid)
        emit("message_out", client=c["shop_name"] if c else cid, text=reply)

    print(f"   AI calls for this message: {brain.CALLS['n']}")
    if not reply:
        print("OUT: (silent: a human is handling this client, message forwarded to staff)")
        return
    print("OUT:", reply.replace("\n", " / "))
    await update.message.reply_text(reply)


# ======================= the weekly stock-audit agent =======================
async def on_audit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Staff sends /audit: the auditor agent runs now (the report arrives in this chat)."""
    if str(update.effective_chat.id) != staff_chat():
        sec(update.effective_user.full_name, "access", "staff access", "block", "/audit from a non-staff chat: ignored")
        return
    note = " ".join(context.args)[:300]
    await update.message.reply_text("\U0001f50e Auditor agent started: checking stock, demand, backorders..."
                                    + (f"\nYour note: {note}" if note else ""))
    await asyncio.to_thread(auditor.run_audit, "requested by " + update.effective_user.first_name, note)


def _audit_id(update, context):
    if context.args:
        return context.args[0]
    reply = update.message.reply_to_message
    m = re.search(r"Audit ID:\s*(AUD-\S+)", reply.text if reply and reply.text else "")
    return m.group(1) if m else None


async def _decide(update: Update, context: ContextTypes.DEFAULT_TYPE, approve: bool):
    if str(update.effective_chat.id) != staff_chat():
        sec(update.effective_user.full_name, "access", "staff access", "block",
            "/approve or /reject from a non-staff chat: ignored (only the team can approve purchases)")
        return
    sec(update.effective_user.full_name, "access", "staff access", "pass", "approval from the registered staff chat")
    aid = _audit_id(update, context)
    if not aid:
        await update.message.reply_text("Reply to the audit report with /approve or /reject (or /approve AUD-...).")
        return
    result = await asyncio.to_thread(auditor.decide, aid, approve, update.effective_user.first_name)
    print(result)
    await update.message.reply_text(result)


async def on_approve(update, context):
    await _decide(update, context, True)


async def on_reject(update, context):
    await _decide(update, context, False)


async def _review(update: Update, context: ContextTypes.DEFAULT_TYPE, approve: bool):
    """Staff clears (/clear) or refuses (/refuse) a flagged pending_review order."""
    if str(update.effective_chat.id) != staff_chat():
        sec(update.effective_user.full_name, "access", "staff access", "block", "/clear or /refuse from a non-staff chat: ignored")
        return
    reply = update.message.reply_to_message
    m = re.search(r"ORD-\d+", " ".join(context.args) or (reply.text if reply and reply.text else ""))
    if not m:
        await update.message.reply_text("Reply /clear or /refuse to the ORDER TO REVIEW alert (or /clear ORD-...).")
        return
    phone, msg, staff_reply = await asyncio.to_thread(review_flagged_order, m.group(0), approve,
                                                      update.effective_user.first_name)
    if phone and msg and phone.startswith("telegram:"):
        await context.bot.send_message(chat_id=int(phone.split(":", 1)[1]), text=msg)
    print(staff_reply)
    await update.message.reply_text(staff_reply)


async def on_clear(update, context):
    await _review(update, context, True)


async def on_refuse(update, context):
    await _review(update, context, False)


async def weekly_audit(context: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(auditor.run_audit, "weekly schedule")


def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("TELEGRAM_TOKEN is missing in .env")
    app = (
        ApplicationBuilder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_read_timeout(40)
        .build()
    )
    app.add_handler(CommandHandler("start", on_start))
    app.add_handler(CommandHandler("reset", on_reset))
    app.add_handler(CommandHandler("staff", on_staff))
    app.add_handler(CommandHandler("done", on_done))
    app.add_handler(CommandHandler("audit", on_audit))
    app.add_handler(CommandHandler("approve", on_approve))
    app.add_handler(CommandHandler("reject", on_reject))
    app.add_handler(CommandHandler("clear", on_clear))
    app.add_handler(CommandHandler("refuse", on_refuse))
    if app.job_queue:            # pip install "python-telegram-bot[job-queue]"
        app.job_queue.run_daily(weekly_audit, days=(1,),          # Monday 08:00 Beirut
                                time=dt.time(8, 0, tzinfo=dt.timezone(dt.timedelta(hours=2))))
        print("Weekly audit scheduled: Mondays 08:00")
    else:
        print("Weekly schedule off (install python-telegram-bot[job-queue]); /audit still works")
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    print("3al Tayer bot is running... (Ctrl+C to stop)")
    app.run_polling(bootstrap_retries=5)


if __name__ == "__main__":
    main()