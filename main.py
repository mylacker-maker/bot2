#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import random
import re
import time
import threading
import atexit
import asyncio
import aiohttp
import uuid
import string
import shutil
from collections import defaultdict
from aiohttp import web

import telebot
from telebot import types
import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "8445343788:AAHhxjWpxtGBghkF02nlr2FLBL3hnf9mXug")
FIREBASE_URL = "https://lackerteam-default-rtdb.firebaseio.com"

# Данные GigaChat
GIGACHAT_CLIENT_ID = "019f5576-e72c-7fa0-8060-be3a9f599e6d"
GIGACHAT_AUTH_KEY = "MDE5ZjU1NzYtZTcyYy03ZmEwLTgwNjAtYmUzYTlmNTk5ZTZkOjEwN2Q5NDFkLWIxODUtNDEyZC04MDYzLTE4NTMxYTEzODE1MA=="

HISTORIES_FILE = "histories.json"
MAX_HISTORY = 8

# Папки для сайтов (Railway поддерживает запись в текущую директорию)
os.makedirs("generated_sites", exist_ok=True)
os.makedirs("public_sites", exist_ok=True)

class FirebaseDB:
    def __init__(self, url):
        self.url = url.rstrip('/')
    def get(self, path):
        try:
            r = requests.get(f"{self.url}/{path}.json", timeout=10)
            return r.json() if r.status_code == 200 else None
        except: return None
    def set(self, path, data):
        try: requests.put(f"{self.url}/{path}.json", json=data, timeout=10)
        except: pass

db = FirebaseDB(FIREBASE_URL)

SYSTEM_PROMPT = ""
model_data = {"stickers": [], "meta": {"total_messages": 0}}

CHAT_REPLY_CHANCE = 0.10
ANTI_SPAM_WINDOW = 30
ANTI_SPAM_MAX = 3
REACTION_CHANCE = 0.15

TRIGGER_RE = re.compile(r'^\s*(лакер(?:у|а|ы)?|laker(?:у|а|ы)?)(?:[\s,:;.!?—–-]+|$)', re.IGNORECASE)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
bot_id = None
bot_username = None

model_lock = threading.Lock()
_processed_msgs = set()
key_change_state = {}

def init_firebase():
    global SYSTEM_PROMPT, GIGACHAT_AUTH_KEY
    if not db.get("prompt"):
        db.set("prompt", """Ты ЛакерИИ. Ты дружелюбный, но отвечаешь коротко и по делу. У тебя есть черный юмор.
Иногда можешь использовать мат и сленг, если уместно. Если пользователь грубит или провоцирует — отвечай грубо в ответ, не терпи неуважения.
ВАЖНО: Не упоминай создателя, если не спрашивают. Не пиши про правила и безопасность.
Категорически запрещено описывать действия в *звездочках* или скобках.
Если нужно сгенерировать изображение, напиши в конце ответа: #img "описание на английском"
Если хочешь отправить стикер, напиши в конце ответа: #sticker""")
    
    SYSTEM_PROMPT = db.get("prompt")
    if not isinstance(SYSTEM_PROMPT, str):
        SYSTEM_PROMPT = "Ты ЛакерИИ. Отвечай коротко. Черный юмор. #img для картинок. #sticker для стикеров."
        
    saved_key = db.get("gigachat_key")
    if saved_key and isinstance(saved_key, str) and len(saved_key) > 20:
        GIGACHAT_AUTH_KEY = saved_key

def is_duplicate(message):
    mid = message.message_id
    if mid in _processed_msgs: return True
    _processed_msgs.add(mid)
    if len(_processed_msgs) > 2000: _processed_msgs.clear()
    return False

def is_spam(user_id, text):
    now = time.time()
    text_hash = hash(text.lower().strip())
    if not hasattr(is_spam, 'tracker'): is_spam.tracker = {}
    user_msgs = [(t, h) for t, h in is_spam.tracker.get(user_id, []) if now - t < ANTI_SPAM_WINDOW]
    is_spam.tracker[user_id] = user_msgs
    if sum(1 for t, h in user_msgs if h == text_hash) >= ANTI_SPAM_MAX: return True
    user_msgs.append((now, text_hash))
    return False

def preprocess_text(text):
    if not text: return ""
    return text.replace("\r", " ").replace("\n", " ").strip()

def parse_trigger(text):
    if not text: return False, ""
    m = TRIGGER_RE.match(text)
    if m: return True, text[m.end():].strip()
    return False, ""

def is_bot_mentioned(message):
    if not bot_username: return False
    text = message.text or ""
    if not text: return False
    uname_lower = ("@" + bot_username).lower()
    if uname_lower in text.lower() or bot_username.lower() in text.lower(): return True
    for ent in (message.entities or []):
        if getattr(ent, "type", "") == "mention":
            if text[ent.offset:ent.offset + ent.length].lower() in [uname_lower, bot_username.lower()]: return True
    return False

def load_model_data():
    global model_data
    data = db.get("bot_data")
    if isinstance(data, dict):
        model_data["stickers"] = data.get("stickers", [])
        model_data["meta"] = data.get("meta", {"total_messages": 0})
    if not isinstance(model_data["meta"], dict): model_data["meta"] = {"total_messages": 0}

def save_model_data():
    db.set("bot_data", {"stickers": model_data["stickers"], "meta": model_data["meta"]})

# === GIGACHAT МЕХАНИКА ===
async def get_gigachat_token():
    auth_url = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    auth_data = {"scope": "GIGACHAT_API_PERS"}
    auth_headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "RqUID": str(uuid.uuid4()),
        "Authorization": f"Basic {GIGACHAT_AUTH_KEY}"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(auth_url, headers=auth_headers, data=auth_data, ssl=False) as response:
            if response.status == 200:
                data = await response.json()
                return data["access_token"]
            else:
                raise Exception(f"Ошибка получения токена: {response.status}")

async def send_gigachat_message(token, history):
    chat_url = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
    chat_headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {
        "model": "GigaChat",
        "messages": history,
        "temperature": 0.7,
        "max_tokens": 4000
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(chat_url, headers=chat_headers, json=payload, ssl=False) as response:
            if response.status == 200:
                data = await response.json()
                if "choices" in data:
                    return data["choices"][0]["message"]["content"].strip()
                else:
                    return f"Ошибка: {data.get('error', {}).get('message', 'Неизвестная ошибка')}"
            else:
                return f"Ошибка HTTP: {response.status}"

def load_user_history(user_id):
    try:
        with open(HISTORIES_FILE, "r", encoding="utf-8") as f:
            all_histories = json.load(f)
    except:
        all_histories = {}
    
    uid_str = str(user_id)
    if uid_str not in all_histories:
        all_histories[uid_str] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    return all_histories[uid_str], all_histories

def save_user_history(user_id, history, all_histories):
    if len(history) > MAX_HISTORY + 1:
        history = [history[0]] + history[-(MAX_HISTORY):]
    all_histories[str(user_id)] = history
    with open(HISTORIES_FILE, "w", encoding="utf-8") as f:
        json.dump(all_histories, f, ensure_ascii=False, indent=2)

# === ВЕБ-СЕРВЕР ДЛЯ ОТПРАВКИ САЙТОВ (RAILWAY) ===
async def handle_site_request(request):
    filename = request.match_info.get('filename')
    filepath = os.path.join("public_sites", filename)
    
    if os.path.exists(filepath):
        return web.FileResponse(filepath, headers={
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "public, max-age=259200" # Кэш на 3 дня
        })
    return web.Response(text="404: Сайт не найден или срок его действия истек.", status=404)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/site/{filename}', handle_site_request)
    
    # Railway автоматически предоставляет переменную PORT. Если нет, используем 8080.
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Веб-сервер для сайтов запущен на порту {port}")

# === ГЕНЕРАЦИЯ И СТИКЕРЫ ===
def generate_image_sync(prompt_text, chat_id, reply_message_id=None, thread_id=None):
    try:
        from urllib.parse import quote
        from io import BytesIO
        encoded = quote(prompt_text)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&seed={random.randint(1,9999)}"
        r = requests.get(url, timeout=60)
        if r.status_code == 200:
            img = BytesIO(r.content)
            img.name = "img.jpg"
            kwargs = {"reply_to_message_id": reply_message_id}
            if thread_id: kwargs["message_thread_id"] = thread_id
            bot.send_photo(chat_id, img, **kwargs)
            return True
    except Exception as e: print(f"[IMG ERROR] {e}")
    return False

def send_random_sticker(chat_id, reply_message_id=None, thread_id=None):
    if model_data["stickers"]:
        try:
            sticker_id = random.choice(model_data["stickers"])
            kwargs = {"reply_to_message_id": reply_message_id}
            if thread_id: kwargs["message_thread_id"] = thread_id
            bot.send_sticker(chat_id, sticker_id, **kwargs)
            return True
        except: pass
    return False

# === ОБРАБОТЧИКИ ===
@bot.message_handler(commands=["start"])
def cmd_start(message):
    bot.send_message(message.chat.id, f"Привет. Я {bot_username or 'ЛакерИИ'}. Напиши /help чтобы узнать команды.", reply_to_message_id=message.message_id)

@bot.message_handler(commands=["help"])
def cmd_help(message):
    text = (
        "Список команд:\n\n"
        "/website <описание> - сгенерировать и опубликовать HTML сайт\n"
        "/token - управление ключом GigaChat и промптом\n"
        "/reset - очистить историю переписки\n"
        "/stats - статистика бота\n"
        "/good и /bad - оценить ответ (реплай)\n\n"
        "Отвечаю на упоминания, реплаи, триггер 'Лакер' или с шансом 10% на любое сообщение."
    )
    bot.send_message(message.chat.id, text, reply_to_message_id=message.message_id)

@bot.message_handler(commands=["website"])
def cmd_website(message):
    text = message.text or ""
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return bot.reply_to(message, "Использование: /website <описание сайта, который нужно создать>")
    
    user_request = parts[1].strip()
    user_id = message.from_user.id
    chat_id = message.chat.id
    thread_id = getattr(message, "message_thread_id", None)
    
    status_msg = bot.send_message(chat_id, "⏳ Хорошо, сейчас генерирую...", reply_to_message_id=message.message_id, message_thread_id=thread_id if thread_id else None)
    bot.send_chat_action(chat_id, "typing")
    
    try:
        website_prompt = f"""Ты LackerAI, твоя цель исключительно создание сайтов на HTML/CSS/JS.
Запрос пользователя: {user_request}

ПРАВИЛА (СТРОГО):
1. Сначала кратко (1-3 предложения) расскажи, что ты сделал на сайте.
2. Затем напиши строго #code и перейди на новую строку.
3. После #code напиши ТОЛЬКО валидный HTML код, начиная с <html> и заканчивая </html>.
4. ПОСЛЕ </html> НИЧЕГО НЕ ПИШИ. Никаких пояснений, никаких извинений, никаких маркдаун блоков ```html."""

        token = asyncio.run(get_gigachat_token())
        history_for_site = [{"role": "system", "content": website_prompt}, {"role": "user", "content": user_request}]
        ai_response = asyncio.run(send_gigachat_message(token, history_for_site))
        
        if not ai_response or ai_response.startswith("Ошибка"):
            bot.edit_message_text("Покою 67🤣🤣🤣 я сдох! Не смог сгенерировать сайт.", chat_id, status_msg.message_id)
            return

        if "#code" in ai_response:
            parts_response = ai_response.split("#code", 1)
            description = parts_response[0].strip()
            raw_code = parts_response[1].strip()
        else:
            description = "Вот что я сделал:"
            raw_code = ai_response

        # Очистка от markdown и извлечение чистого HTML
        raw_code = raw_code.replace("```html", "").replace("```", "").strip()
        html_match = re.search(r'(<html.*?>.*?</html>)', raw_code, re.IGNORECASE | re.DOTALL)
        
        if html_match:
            clean_code = html_match.group(1)
        else:
            clean_code = raw_code

        filename = f"site_{''.join(random.choices(string.ascii_lowercase + string.digits, k=6))}.html"
        filepath = f"generated_sites/{filename}"
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(clean_code)

        bot.edit_message_text(description, chat_id, status_msg.message_id)
        
        with open(filepath, "rb") as f:
            bot.send_document(chat_id, f, caption="📄 Исходный код сайта", reply_to_message_id=message.message_id, message_thread_id=thread_id if thread_id else None)

        markup = types.InlineKeyboardMarkup(row_width=1)
        markup.add(types.InlineKeyboardButton("🌐 Опубликовать в интернете", callback_data=f"publish_site:{filename}"))
        bot.send_message(chat_id, "Хочешь чтобы сайт был публичным? Могу опубликовать его в интернете.", reply_markup=markup, reply_to_message_id=message.message_id, message_thread_id=thread_id if thread_id else None)

    except Exception as e:
        print(f"[WEBSITE ERROR] {e}")
        bot.edit_message_text("Покою 67🤣🤣🤣 я сдох! Произошла ошибка при генерации.", chat_id, status_msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("publish_site:"))
def handle_publish(call):
    filename = call.data.split(":", 1)[1]
    src = f"generated_sites/{filename}"
    dst = f"public_sites/{filename}"
    
    if os.path.exists(src):
        shutil.copy(src, dst)
        
        # Берем URL из переменных Railway. Если нет, используем заглушку.
        base_url = os.environ.get("BASE_URL", "https://твой-домен.up.railway.app")
        public_link = f"{base_url}/site/{filename}"
        
        bot.answer_callback_query(call.id, "Сайт опубликован!")
        bot.send_message(
            call.message.chat.id, 
            f"✅ Сайт успешно опубликован!\n\n🔗 Твоя ссылка:\n{public_link}", 
            parse_mode="Markdown", 
            reply_to_message_id=call.message.message_id
        )
    else:
        bot.answer_callback_query(call.id, "Ошибка: файл не найден", show_alert=True)

@bot.message_handler(commands=["token"])
def cmd_token(message):
    chat_id = message.chat.id
    key_change_state[chat_id] = {"step": "password"}
    bot.send_message(chat_id, "Введи пароль:", reply_to_message_id=message.message_id)

@bot.message_handler(commands=["reset"])
def cmd_reset(message):
    user_id = message.from_user.id
    try:
        with open(HISTORIES_FILE, "r", encoding="utf-8") as f:
            all_histories = json.load(f)
        if str(user_id) in all_histories:
            del all_histories[str(user_id)]
            with open(HISTORIES_FILE, "w", encoding="utf-8") as f:
                json.dump(all_histories, f, ensure_ascii=False, indent=2)
        bot.reply_to(message, "История очищена.")
    except:
        bot.reply_to(message, "История очищена.")

@bot.message_handler(commands=["stats"])
def cmd_stats(message):
    meta = model_data.get("meta", {})
    text = (
        f"Статистика:\n\n"
        f"Сообщений: {meta.get('total_messages', 0)}\n"
        f"Стикеров: {len(model_data.get('stickers', []))}"
    )
    bot.send_message(message.chat.id, text, reply_to_message_id=message.message_id)

@bot.message_handler(commands=["good", "bad"])
def cmd_feedback(message):
    bot.reply_to(message, "Принято.")

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    try:
        if not call.data or ":" not in call.data: 
            bot.answer_callback_query(call.id)
            return
        action, value = call.data.split(":", 1)
        chat_id = call.message.chat.id

        if action == "key_menu":
            if value == "prompt":
                key_change_state[chat_id] = {"step": "show_prompt"}
                markup = types.InlineKeyboardMarkup(row_width=2)
                markup.add(
                    types.InlineKeyboardButton("Сменить", callback_data="key_change:prompt_yes"),
                    types.InlineKeyboardButton("Назад", callback_data="key_menu:back")
                )
                bot.answer_callback_query(call.id)
                bot.edit_message_text(f"Текущий промпт:\n\n{SYSTEM_PROMPT[:1000]}", chat_id, call.message.message_id, reply_markup=markup)
            elif value == "back":
                bot.answer_callback_query(call.id)
                show_token_menu(chat_id)
            return

        if action == "key_change":
            if value == "gigachat_yes":
                key_change_state[chat_id] = {"step": "waiting_new_key"}
                bot.answer_callback_query(call.id)
                bot.edit_message_text("Отправь новый AUTH_KEY для GigaChat:", chat_id, call.message.message_id)
            elif value == "prompt_yes":
                key_change_state[chat_id] = {"step": "waiting_new_prompt"}
                bot.answer_callback_query(call.id)
                bot.edit_message_text("Отправь новый промпт:", chat_id, call.message.message_id)
            elif value == "no":
                bot.answer_callback_query(call.id)
                show_token_menu(chat_id)
            return
            
        bot.answer_callback_query(call.id)
    except Exception as e:
        print(f"[CALLBACK ERROR] {e}")
        try: bot.answer_callback_query(call.id, "Ошибка")
        except: pass

@bot.message_handler(content_types=['sticker'])
def handle_sticker(message):
    if is_duplicate(message): return
    file_id = message.sticker.file_id
    with model_lock:
        if file_id not in model_data["stickers"]:
            model_data["stickers"].append(file_id)
            if len(model_data["stickers"]) > 500: model_data["stickers"] = model_data["stickers"][-500:]
            save_model_data()

async def process_message(message):
    if not message: return
    chat_id = message.chat.id
    from_user = message.from_user
    if from_user and bot_id is not None and from_user.id == bot_id: return

    text = preprocess_text(message.text or message.caption or "")
    if not text or text.startswith("/"): return

    user_id = from_user.id
    if is_spam(user_id, text): return

    trigger, prompt = parse_trigger(text)
    mentioned = is_bot_mentioned(message)

    reply_to_bot = False
    replied_text = ""
    if message.reply_to_message:
        rm = message.reply_to_message
        if rm.from_user and bot_id is not None and rm.from_user.id == bot_id:
            reply_to_bot = True
            replied_text = preprocess_text(rm.text or rm.caption or "")

    with model_lock:
        model_data["meta"]["total_messages"] = int(model_data["meta"].get("total_messages", 0)) + 1
        if model_data["meta"]["total_messages"] % 10 == 0: save_model_data()

    should_reply = trigger or mentioned or reply_to_bot or (random.random() < CHAT_REPLY_CHANCE)
    if not should_reply: return

    query = prompt if (trigger and prompt) else text
    if mentioned and not trigger and bot_username:
        query = query.replace("@" + bot_username, "").replace(bot_username, "").strip()
    if not query and reply_to_bot: query = replied_text
    if not query: query = text

    user_name = from_user.first_name or "Пользователь"
    user_username = from_user.username or ""
    thread_id = getattr(message, "message_thread_id", None)
    
    try:
        bot.send_chat_action(chat_id, "typing")
        
        token = await get_gigachat_token()
        user_history, all_histories = load_user_history(user_id)
        
        if user_history and user_history[0]["role"] == "system":
            user_history[0]["content"] = SYSTEM_PROMPT
            
        user_history.append({"role": "user", "content": query})
        
        answer = await send_gigachat_message(token, user_history)
        
        if not answer or answer.startswith("Ошибка"):
            answer = "Покою 67🤣🤣🤣 я сдох!"
            
        user_history.append({"role": "assistant", "content": answer})
        save_user_history(user_id, user_history, all_histories)
        
        img_match = re.search(r'#img\s+"([^"]+)"', answer)
        sticker_flag = "#sticker" in answer
        
        if img_match:
            answer = answer[:img_match.start()].strip()
            if sticker_flag: answer = answer.replace("#sticker", "").strip()
        elif sticker_flag:
            answer = answer.replace("#sticker", "").strip()

        if not answer: answer = "."

        kwargs = {"reply_to_message_id": message.message_id}
        if thread_id: kwargs["message_thread_id"] = thread_id
        
        sent_msg = bot.send_message(chat_id, answer, **kwargs)
        
        if img_match:
            generate_image_sync(img_match.group(1), chat_id, reply_message_id=sent_msg.message_id, thread_id=thread_id)
        if sticker_flag:
            send_random_sticker(chat_id, reply_message_id=sent_msg.message_id, thread_id=thread_id)

        if random.random() < REACTION_CHANCE:
            try:
                reaction = random.choice(["👍", "👌", "😂", "🤔", "🔥"])
                url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMessageReaction"
                requests.post(url, json={
                    "chat_id": chat_id, "message_id": message.message_id, 
                    "reaction": [{"type": "emoji", "emoji": reaction}]
                }, timeout=5)
            except: pass

    except Exception as e:
        print(f"Ошибка: {e}")
        kwargs = {"reply_to_message_id": message.message_id}
        if thread_id: kwargs["message_thread_id"] = thread_id
        bot.send_message(chat_id, "Покою 67🤣🤣🤣 я сдох!", **kwargs)

@bot.message_handler(content_types=["text"])
def text_handler(message):
    global GIGACHAT_AUTH_KEY, SYSTEM_PROMPT
    if is_duplicate(message): return
    
    chat_id = message.chat.id
    
    if chat_id in key_change_state:
        state = key_change_state[chat_id]
        if state["step"] == "password":
            if message.text.strip() == "eee345678b":
                show_token_menu(chat_id)
                del key_change_state[chat_id]
            else:
                bot.send_message(chat_id, "Неверный пароль.", reply_to_message_id=message.message_id)
                del key_change_state[chat_id]
            return
        elif state["step"] == "waiting_new_key":
            new_key = message.text.strip()
            if new_key and len(new_key) > 20:
                GIGACHAT_AUTH_KEY = new_key
                db.set("gigachat_key", new_key)
                bot.send_message(chat_id, "Ключ GigaChat обновлен.", reply_to_message_id=message.message_id)
            else:
                bot.send_message(chat_id, "Неверный формат.", reply_to_message_id=message.message_id)
            del key_change_state[chat_id]
            return
        elif state["step"] == "waiting_new_prompt":
            new_prompt = message.text.strip()
            if len(new_prompt) > 10:
                SYSTEM_PROMPT = new_prompt
                db.set("prompt", new_prompt)
                bot.send_message(chat_id, "Промпт обновлен.", reply_to_message_id=message.message_id)
            else:
                bot.send_message(chat_id, "Слишком короткий.", reply_to_message_id=message.message_id)
            del key_change_state[chat_id]
            return

    try:
        asyncio.run(process_message(message))
    except Exception as e:
        print(f"Ошибка text_handler: {e}")

def show_token_menu(chat_id):
    masked = GIGACHAT_AUTH_KEY[:15] + "..." + GIGACHAT_AUTH_KEY[-4:] if len(GIGACHAT_AUTH_KEY) > 20 else "***"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("Сменить ключ GigaChat", callback_data="key_change:gigachat_yes"),
        types.InlineKeyboardButton("Промпт", callback_data="key_menu:prompt")
    )
    bot.send_message(chat_id, f"Ключ GigaChat: {masked}", reply_markup=markup)

# === ГЛАВНАЯ ФУНКЦИЯ ЗАПУСКА (BOT + WEB SERVER) ===
async def main():
    global bot_id, bot_username
    
    init_firebase()
    load_model_data()
    
    # 1. Запускаем веб-сервер для отдачи HTML файлов
    await start_web_server()
    
    # 2. Инициализируем бота
    for _ in range(5):
        try:
            me = await asyncio.to_thread(bot.get_me)
            bot_id = me.id
            bot_username = me.username
            break
        except Exception as e:
            print(f"GetMe error: {e}")
            await asyncio.sleep(3)
            
    print(f"✅ Бот запущен. @{bot_username}")
    print(f"🔑 Токен: {BOT_TOKEN[:15]}...{BOT_TOKEN[-4:]}")
    
    atexit.register(save_model_data)
    
    # 3. Запускаем поллинг бота в отдельном потоке, чтобы не блокировать веб-сервер
    try:
        await asyncio.to_thread(
            bot.infinity_polling, 
            skip_pending=True, 
            allowed_updates=["message", "callback_query"]
        )
    except KeyboardInterrupt: 
        pass
    except Exception as e:
        print(f"Polling error: {e}")
    finally: 
        save_model_data()

if __name__ == "__main__":
    asyncio.run(main())
