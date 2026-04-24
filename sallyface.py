import sqlite3
import re
import time
import threading
from datetime import datetime
import telebot
from telebot import types
from functools import wraps
from flask import Flask

# ========== ВЕБ-СЕРВЕР ДЛЯ RENDER ==========
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    app.run(host='0.0.0.0', port=10000)

threading.Thread(target=run_flask, daemon=True).start()

# ========== НАСТРОЙКИ ==========
TOKEN = '8311159073:AAGqEK7o0dKcZYcZyDtyg1XMLGW5vRnVaNc'
CHANNEL_ID = -1001736748377
CHANNEL_LINK = 'https://t.me/+zaFNqjGZI7I3ZjRi'
INVITE_LINK = 'https://t.me/+cbU7L_baCWs3YzVi'
GROUP_ID = -1003783177890
ADMIN_IDS = [8245074982, 600630325, 7473678819]
UNTOUCHABLE_USER_ID = 600630325
# =================================

bot = telebot.TeleBot(TOKEN)

# --- База данных с пулом соединений ---
DB_PATH = 'users.db'
db_lock = threading.Lock()

def get_db_connection():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            nickname TEXT UNIQUE,
            last_change TIMESTAMP,
            reputation INTEGER DEFAULT 0
        )
    ''')
    try:
        cursor.execute('ALTER TABLE users ADD COLUMN reputation INTEGER DEFAULT 0')
    except: pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS bans (
            user_id INTEGER PRIMARY KEY,
            ban_until TIMESTAMP,
            reason TEXT,
            banned_by INTEGER,
            ban_time TIMESTAMP
        )
    ''')
    try:
        cursor.execute('ALTER TABLE bans ADD COLUMN banned_by INTEGER')
    except: pass
    try:
        cursor.execute('ALTER TABLE bans ADD COLUMN ban_time TIMESTAMP')
    except: pass

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS admin_actions_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id INTEGER,
            action_type TEXT,
            target_id INTEGER,
            details TEXT,
            action_time TIMESTAMP
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nickname_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            reason TEXT,
            status TEXT DEFAULT 'pending',
            request_time TIMESTAMP,
            last_reject_time TIMESTAMP DEFAULT 0
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS anonymous_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            nickname TEXT,
            media_group_id TEXT,
            file_ids TEXT,
            caption TEXT,
            channel_message_ids TEXT,
            likes INTEGER DEFAULT 0,
            dislikes INTEGER DEFAULT 0,
            status TEXT DEFAULT 'pending',
            post_time TIMESTAMP,
            as_admin INTEGER DEFAULT 0,
            media_type TEXT DEFAULT 'text'
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS post_votes (
            user_id INTEGER,
            post_id INTEGER,
            vote_type TEXT,
            PRIMARY KEY (user_id, post_id)
        )
    ''')

    conn.commit()
    conn.close()

init_db()

# ========== ХРАНИЛИЩА ==========
user_command_history = {}
user_states = {}
user_media_groups = {}

# ========== АНТИСПАМ КОМАНД ==========
def check_command_spam(user_id, command):
    now = int(time.time())
    if user_id not in user_command_history:
        user_command_history[user_id] = []
    user_command_history[user_id] = [(cmd, ts) for cmd, ts in user_command_history[user_id] if now - ts < 60]
    user_command_history[user_id].append((command, now))
    if len(user_command_history[user_id]) >= 10:
        last_ten = user_command_history[user_id][-10:]
        if all(cmd == last_ten[0][0] for cmd, _ in last_ten):
            user_command_history[user_id] = []
            return True, "Спам одной и той же командой (10 раз подряд)"
    return False, None

# ========== ДЕКОРАТОР ПРОВЕРКИ ПОДПИСКИ И АНТИСПАМА ==========
def require_subscription_and_antispam(command_name=None):
    def decorator(func):
        @wraps(func)
        def wrapper(message, *args, **kwargs):
            user_id = message.from_user.id
            banned, ban_reason, banned_by, ban_time = is_banned(user_id)
            if banned:
                ban_until = get_ban_until(user_id)
                if ban_until == -1:
                    bot.send_message(message.chat.id, "❌ Вы забанены навсегда. Доступ запрещён.")
                else:
                    remaining = max(0, ban_until - int(time.time()))
                    if remaining < 3600:
                        minutes = remaining // 60
                        bot.send_message(message.chat.id, f"❌ Вы забанены на {minutes} минут. Доступ запрещён.")
                    else:
                        days = remaining // (24 * 3600)
                        hours = (remaining % (24 * 3600)) // 3600
                        bot.send_message(message.chat.id, f"❌ Вы забанены на {days} дн. {hours} ч. Доступ запрещён.")
                return

            cmd_name = command_name or (message.text if hasattr(message, 'text') and message.text else 'unknown')
            is_spam, spam_reason = check_command_spam(user_id, cmd_name)
            if is_spam:
                ban_user(user_id, duration_minutes=10, reason=spam_reason, banned_by=bot.get_me().id)
                log_admin_action(bot.get_me().id, "AUTO_BAN_SPAM", user_id, f"10 минут, причина: {spam_reason}")
                notify_group(f"<blockquote>🚨 Автоматический бан\nПользователь {user_id}\nПричина: {spam_reason}\nСрок: 10 минут</blockquote>")
                bot.send_message(message.chat.id, "🚫 Вы забанены на 10 минут за спам командами.")
                return

            if user_id not in ADMIN_IDS and not check_subscription(user_id):
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("🔗 Вступить в канал", url=INVITE_LINK))
                markup.add(types.InlineKeyboardButton("✅ Проверить подписку", callback_data='check_subscription_from_command'))
                bot.send_message(message.chat.id,
                    "❌ Вы не подписаны на канал!\n\n"
                    "Для использования бота необходимо подписаться на наш Telegram-канал.\n\n"
                    "После подписки нажмите кнопку «Проверить подписку».",
                    reply_markup=markup)
                return

            return func(message, *args, **kwargs)
        return wrapper
    return decorator

# ========== ФУНКЦИИ ПОЛЬЗОВАТЕЛЕЙ ==========
def get_user_nickname(user_id):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT nickname FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def get_user_reputation(user_id):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT reputation FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0

def add_reputation(user_id, delta):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET reputation = reputation + ? WHERE user_id = ?', (delta, user_id))
        conn.commit()
        conn.close()

def set_user_nickname(user_id, nickname):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('INSERT INTO users (user_id, nickname, last_change, reputation) VALUES (?, ?, ?, 0)',
                           (user_id, nickname, int(time.time())))
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            conn.close()
            return False

def update_user_nickname(user_id, nickname):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE users SET nickname = ?, last_change = ? WHERE user_id = ?',
                       (nickname, int(time.time()), user_id))
        conn.commit()
        conn.close()
        return True

def remove_user_nickname(user_id):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM users WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
        return True

def can_change_nickname(user_id):
    if user_id in ADMIN_IDS:
        return True, None
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT last_change FROM users WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        if not row:
            return True, None
        last = row[0]
        week_ago = int(time.time()) - 7 * 24 * 3600
        if last < week_ago:
            return True, None
        else:
            next_change = datetime.fromtimestamp(last + 7 * 24 * 3600).strftime('%Y-%m-%d %H:%M:%S')
            return False, next_change

def validate_nickname_base(base):
    if len(base) < 4 or len(base) > 12:
        return False, "Ник должен содержать от 4 до 12 символов"
    if not re.match(r'^[a-zA-Z0-9_]+$', base):
        return False, "Разрешены только латинские буквы, цифры и символ _"
    if base.count('_') > 4:
        return False, "Символ _ можно использовать не более 4 раз"
    return True, "OK"

# ========== ФУНКЦИИ БАНОВ ==========
def is_banned(user_id):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT ban_until, reason, banned_by, ban_time FROM bans WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return (False, None, None, None)
        ban_until, reason, banned_by, ban_time = row
        if ban_until == -1:
            conn.close()
            return (True, reason, banned_by, ban_time)
        if ban_until > int(time.time()):
            conn.close()
            return (True, reason, banned_by, ban_time)
        else:
            cursor.execute('DELETE FROM bans WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
            return (False, None, None, None)

def ban_user(user_id, duration_days=None, duration_minutes=None, reason="Не указана", banned_by=None, add_time=False):
    now = int(time.time())
    if duration_minutes is not None:
        duration_seconds = duration_minutes * 60
    elif duration_days is not None:
        duration_seconds = duration_days * 24 * 3600
    else:
        duration_seconds = None

    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        if add_time:
            cursor.execute('SELECT ban_until, reason FROM bans WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            if row:
                current_until, old_reason = row
                if current_until == -1:
                    conn.close()
                    return False, "У пользователя уже перманентный бан. Сначала разбаньте его."
                remaining = max(0, current_until - now)
                if duration_seconds is not None:
                    new_until = now + remaining + duration_seconds
                else:
                    new_until = -1
            else:
                if duration_seconds is not None:
                    new_until = now + duration_seconds
                else:
                    new_until = -1
        else:
            if duration_seconds is not None:
                new_until = now + duration_seconds
            else:
                new_until = -1

        if add_time and row:
            reason = f"{old_reason} | {reason}"

        cursor.execute('INSERT OR REPLACE INTO bans (user_id, ban_until, reason, banned_by, ban_time) VALUES (?, ?, ?, ?, ?)',
                       (user_id, new_until, reason, banned_by, now))
        conn.commit()
        conn.close()

    try:
        if new_until == -1:
            text = f"🚫 Вы забанены навсегда. Причина: {reason}"
        else:
            remaining_sec = new_until - now
            if remaining_sec < 3600:
                minutes = remaining_sec // 60
                text = f"🚫 Вы забанены на {minutes} минут. Причина: {reason}"
            else:
                days_left = remaining_sec // (24 * 3600)
                hours_left = (remaining_sec % (24 * 3600)) // 3600
                text = f"🚫 Вы забанены на {days_left} дн. {hours_left} ч. Причина: {reason}"
        bot.send_message(user_id, text)
    except:
        pass
    return True, None

def unban_user(user_id):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM bans WHERE user_id = ?', (user_id,))
        conn.commit()
        conn.close()
    try:
        bot.send_message(user_id, "✅ Вы были разбанены администратором. Теперь вы снова можете пользоваться ботом.")
    except:
        pass

def get_all_bans():
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, ban_until, reason, banned_by, ban_time FROM bans')
        rows = cursor.fetchall()
        conn.close()
        return rows

def get_ban_until(user_id):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT ban_until FROM bans WHERE user_id = ?', (user_id,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def get_remaining_ban_time(user_id):
    until = get_ban_until(user_id)
    if until == -1:
        return float('inf')
    return max(0, until - int(time.time()))

# ========== ОБЩИЕ ФУНКЦИИ ==========
def get_all_users():
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT user_id, nickname FROM users')
        rows = cursor.fetchall()
        conn.close()
        return rows

def log_admin_action(admin_id, action_type, target_id, details=""):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT INTO admin_actions_log (admin_id, action_type, target_id, details, action_time) VALUES (?, ?, ?, ?, ?)',
                       (admin_id, action_type, target_id, details, int(time.time())))
        conn.commit()
        conn.close()

# ========== ЗАЯВКИ НА УДАЛЕНИЕ НИКА ==========
def can_request_delete(user_id):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT last_reject_time FROM nickname_requests WHERE user_id = ? ORDER BY last_reject_time DESC LIMIT 1', (user_id,))
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            last_reject = row[0]
            if int(time.time()) - last_reject < 24 * 3600:
                remaining = 24 * 3600 - (int(time.time()) - last_reject)
                hours = remaining // 3600
                minutes = (remaining % 3600) // 60
                return False, f"{hours} ч {minutes} мин"
        return True, None

def create_delete_request(user_id, reason):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO nickname_requests (user_id, reason, status, request_time, last_reject_time)
            VALUES (?, ?, 'pending', ?, 0)
        ''', (user_id, reason, int(time.time())))
        conn.commit()
        last_id = cursor.lastrowid
        conn.close()
        return last_id

def get_request_by_id(request_id):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, user_id, reason, status, request_time FROM nickname_requests WHERE id = ?', (request_id,))
        row = cursor.fetchone()
        if row:
            if row[3] == 'pending' and (int(time.time()) - row[4]) > 24 * 3600:
                cursor.execute('UPDATE nickname_requests SET status = "expired" WHERE id = ?', (request_id,))
                conn.commit()
                conn.close()
                return None
        conn.close()
        return row

def update_request_status(request_id, status, update_reject_time=False):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        if update_reject_time and status == 'rejected':
            cursor.execute('UPDATE nickname_requests SET status = ?, last_reject_time = ? WHERE id = ?',
                           (status, int(time.time()), request_id))
        else:
            cursor.execute('UPDATE nickname_requests SET status = ? WHERE id = ?', (status, request_id))
        conn.commit()
        conn.close()

def expire_old_requests():
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        now = int(time.time())
        expire_time = now - 24 * 3600
        cursor.execute('''
            UPDATE nickname_requests 
            SET status = 'expired' 
            WHERE status = 'pending' AND request_time < ?
        ''', (expire_time,))
        count = cursor.rowcount
        conn.commit()
        conn.close()
        return count

# ========== ФУНКЦИИ ДЛЯ АНОНИМНЫХ ПОСТОВ ==========
def save_anonymous_post(user_id, nickname, media_group_id, file_ids, caption, as_admin=False, media_type='text'):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO anonymous_posts 
            (user_id, nickname, media_group_id, file_ids, caption, status, post_time, as_admin, media_type)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
        ''', (
            user_id, nickname, media_group_id,
            ','.join(file_ids) if isinstance(file_ids, list) else file_ids,
            caption, int(time.time()), 1 if as_admin else 0, media_type
        ))
        conn.commit()
        post_id = cursor.lastrowid
        conn.close()
        return post_id

def update_post_status(post_id, status, channel_message_ids=None):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        if channel_message_ids:
            ids_str = ','.join(str(mid) for mid in channel_message_ids) if isinstance(channel_message_ids, list) else str(channel_message_ids)
            cursor.execute('UPDATE anonymous_posts SET status = ?, channel_message_ids = ? WHERE id = ?',
                           (status, ids_str, post_id))
        else:
            cursor.execute('UPDATE anonymous_posts SET status = ? WHERE id = ?', (status, post_id))
        conn.commit()
        conn.close()

def get_post_by_id(post_id):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, user_id, nickname, media_group_id, file_ids, caption, status, as_admin, media_type 
            FROM anonymous_posts WHERE id = ?
        ''', (post_id,))
        row = cursor.fetchone()
        conn.close()
        return row

def get_vote(post_id, user_id):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT vote_type FROM post_votes WHERE post_id = ? AND user_id = ?', (post_id, user_id))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None

def set_vote(post_id, user_id, vote_type):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO post_votes (post_id, user_id, vote_type) VALUES (?, ?, ?)',
                       (post_id, user_id, vote_type))
        conn.commit()
        conn.close()

def update_post_likes(post_id):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM post_votes WHERE post_id = ? AND vote_type = "like"', (post_id,))
        likes = cursor.fetchone()[0]
        cursor.execute('SELECT COUNT(*) FROM post_votes WHERE post_id = ? AND vote_type = "dislike"', (post_id,))
        dislikes = cursor.fetchone()[0]
        cursor.execute('UPDATE anonymous_posts SET likes = ?, dislikes = ? WHERE id = ?', (likes, dislikes, post_id))
        conn.commit()
        conn.close()
        return likes, dislikes

def get_vote_keyboard(post_id):
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT likes, dislikes FROM anonymous_posts WHERE id = ?', (post_id,))
        row = cursor.fetchone()
        conn.close()
        likes, dislikes = row if row else (0, 0)
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton(f"👍 {likes}", callback_data=f'vote_{post_id}_like'),
        types.InlineKeyboardButton(f"👎 {dislikes}", callback_data=f'vote_{post_id}_dislike')
    )
    return markup

def get_post_moderation_keyboard(post_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("✅ Одобрить", callback_data=f'approve_post_{post_id}'),
        types.InlineKeyboardButton("❌ Отказать", callback_data=f'reject_post_{post_id}')
    )
    return markup

# ========== ЗАЩИТА НЕПРИКАСАЕМОГО ==========
def check_untouchable(target_id, admin_id, action_type):
    if target_id == UNTOUCHABLE_USER_ID and admin_id != UNTOUCHABLE_USER_ID:
        if action_type == "разбан":
            return False, "owner_unban_attempt"
        ban_user(admin_id, duration_days=None, reason=f"Попытка {action_type} неприкасаемого пользователя", banned_by=UNTOUCHABLE_USER_ID)
        log_admin_action(admin_id, f"ATTEMPT_{action_type}", target_id, f"Автоматический перманентный бан за попытку {action_type}")
        notify_group(f"<blockquote>🚨 Администратор {admin_id} попытался {action_type} неприкасаемого пользователя!\n❌ ЗАБАНЕН НАВСЕГДА</blockquote>")
        try:
            markup = types.InlineKeyboardMarkup()
            markup.add(
                types.InlineKeyboardButton("✅ Разбанить админа", callback_data=f'owner_unban_{admin_id}_{action_type}_{int(time.time())}'),
                types.InlineKeyboardButton("🔨 Оставить бан", callback_data=f'owner_keepban_{admin_id}_{action_type}_{int(time.time())}')
            )
            action_text = {"бан": "забанить", "удаление ника": "удалить ник", "разбан": "разбанить", "написать": "написать"}.get(action_type, action_type)
            bot.send_message(UNTOUCHABLE_USER_ID,
                f"⚠️ <b>ВНИМАНИЕ! ПОПЫТКА АТАКИ!</b>\n\n"
                f"👤 Администратор <code>{admin_id}</code>\n"
                f"🎯 Пытался: {action_text} вас\n"
                f"⏰ Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"❌ Администратор <b>ЗАБАНЕН НАВСЕГДА</b>\n\n"
                f"Выберите действие:",
                parse_mode='HTML', reply_markup=markup)
        except Exception as e:
            print(f"Не удалось отправить уведомление владельцу: {e}")
        if admin_id in ADMIN_IDS:
            ADMIN_IDS.remove(admin_id)
        return True, admin_id
    return False, None

def handle_admin_vs_admin_ban(admin_id, target_id, reason):
    ban_user(admin_id, duration_days=None, reason=f"Попытка забанить администратора {target_id}", banned_by=UNTOUCHABLE_USER_ID)
    ban_user(target_id, duration_days=None, reason=f"Был забанен администратором {admin_id} (оба забанены)", banned_by=UNTOUCHABLE_USER_ID)
    log_admin_action(admin_id, "BAN_ADMIN", target_id, f"Забанил админа, оба забанены. Причина: {reason}")
    notify_group(f"<blockquote>🚨 Администратор {admin_id} попытался забанить администратора {target_id}!\n❌ ОБА ЗАБАНЕНЫ НАВСЕГДА</blockquote>")
    if admin_id in ADMIN_IDS:
        ADMIN_IDS.remove(admin_id)
    if target_id in ADMIN_IDS:
        ADMIN_IDS.remove(target_id)
    try:
        timestamp = int(time.time())
        markup = types.InlineKeyboardMarkup(row_width=2)
        markup.add(
            types.InlineKeyboardButton(f"✅ Разбанить {admin_id} (кто банил)", callback_data=f'owner_unban_admin1_{admin_id}_{target_id}_{timestamp}'),
            types.InlineKeyboardButton(f"🔨 Оставить бан {admin_id}", callback_data=f'owner_keepban_admin1_{admin_id}_{target_id}_{timestamp}')
        )
        markup.add(
            types.InlineKeyboardButton(f"✅ Разбанить {target_id} (кого банили)", callback_data=f'owner_unban_admin2_{admin_id}_{target_id}_{timestamp}'),
            types.InlineKeyboardButton(f"🔨 Оставить бан {target_id}", callback_data=f'owner_keepban_admin2_{admin_id}_{target_id}_{timestamp}')
        )
        markup.add(types.InlineKeyboardButton("✅ Разбанить ОБОИХ", callback_data=f'owner_unban_both_{admin_id}_{target_id}_{timestamp}'))
        bot.send_message(UNTOUCHABLE_USER_ID,
            f"⚠️ <b>ВНИМАНИЕ! АДМИН ЗАБАНИЛ АДМИНА!</b>\n\n"
            f"👤 Администратор <code>{admin_id}</code>\n"
            f"🎯 Забанил администратора <code>{target_id}</code>\n"
            f"📋 Причина: {reason}\n"
            f"⏰ Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"❌ <b>ОБА АДМИНИСТРАТОРА ЗАБАНЕНЫ НАВСЕГДА</b>\n\n"
            f"Выберите действие:",
            parse_mode='HTML', reply_markup=markup)
    except Exception as e:
        print(f"Не удалось отправить уведомление владельцу: {e}")

# ========== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==========
def check_subscription(user_id):
    try:
        member = bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status != 'left'
    except:
        return False

def notify_group(text, parse_mode='HTML', reply_markup=None):
    try:
        bot.send_message(GROUP_ID, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except:
        pass

def escape_html(text):
    if not text:
        return ""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

# ========== ФУНКЦИИ СОСТОЯНИЙ ==========
def set_state(user_id, key, value):
    if user_id not in user_states:
        user_states[user_id] = {}
    user_states[user_id][key] = value

def get_state(user_id, key):
    return user_states.get(user_id, {}).get(key)

def clear_state(user_id, key=None):
    if user_id in user_states:
        if key:
            user_states[user_id].pop(key, None)
        else:
            del user_states[user_id]

# ========== КЛАВИАТУРЫ ==========
def user_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("💻 Личный кабинет"))
    markup.add(types.KeyboardButton("✍️ Написать администраторам"))
    markup.add(types.KeyboardButton("✏️ Удалить псевдоним"))
    markup.add(types.KeyboardButton("🏴‍☠️ Анонимный шепотом вещает.."), types.KeyboardButton("❓ FAQ"))
    return markup

def no_nick_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("📝 Создать псевдоним"))
    markup.add(types.KeyboardButton("🏴‍☠️ Анонимный шепотом вещает.."), types.KeyboardButton("❓ FAQ"))
    return markup

def admin_main_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add(types.KeyboardButton("🔧 Админ-панель"))
    markup.add(types.KeyboardButton("🏴‍☠️ Анонимный шепотом вещает.."), types.KeyboardButton("❓ FAQ"))
    return markup

def require_nick_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📝 Создать псевдоним", callback_data='create_nick_from_require'))
    return markup

def get_admin_main_inline_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🎯 Действия", callback_data='admin_actions_tab'))
    markup.add(types.InlineKeyboardButton("📋 Список пользователей", callback_data='admin_users_tab'))
    markup.add(types.InlineKeyboardButton("📊 Статистика", callback_data='admin_stats_tab'))
    markup.add(types.InlineKeyboardButton("ℹ️ INFO", callback_data='admin_info_tab'))
    return markup

def get_admin_actions_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("🔨 Выдать бан", callback_data='admin_ban_menu'),
        types.InlineKeyboardButton("✅ Разбан", callback_data='admin_unban')
    )
    markup.add(
        types.InlineKeyboardButton("🗑 Удалить ник", callback_data='admin_remove_nick'),
        types.InlineKeyboardButton("💬 Написать пользователю", callback_data='admin_msg_user')
    )
    markup.add(
        types.InlineKeyboardButton("📋 Список банов", callback_data='admin_banlist'),
        types.InlineKeyboardButton("📢 Рассылка", callback_data='admin_broadcast')
    )
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data='admin_back_to_main'))
    return markup

def get_ban_duration_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("⛔ Бан 1 день", callback_data='ban_dur_1'),
        types.InlineKeyboardButton("⛔ Бан 7 дней", callback_data='ban_dur_7')
    )
    markup.add(types.InlineKeyboardButton("⛔ Бан навсегда", callback_data='ban_dur_perm'))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data='admin_actions_tab'))
    return markup

def get_admin_users_keyboard(users, page=0):
    items_per_page = 5
    total_pages = (len(users) + items_per_page - 1) // items_per_page
    start = page * items_per_page
    end = start + items_per_page
    page_users = users[start:end]
    markup = types.InlineKeyboardMarkup()
    for row in page_users:
        uid, nick = row[0], row[1]
        markup.add(types.InlineKeyboardButton(f"{nick} (ID: {uid})", callback_data=f'admin_user_{uid}'))
    nav_buttons = []
    if page > 0:
        nav_buttons.append(types.InlineKeyboardButton("◀️", callback_data=f'admin_users_page_{page-1}'))
    if page < total_pages - 1:
        nav_buttons.append(types.InlineKeyboardButton("▶️", callback_data=f'admin_users_page_{page+1}'))
    if nav_buttons:
        markup.row(*nav_buttons)
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data='admin_back_to_main'))
    return markup

def get_admin_user_actions_keyboard(target_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("⛔ Бан 1 день", callback_data=f'admin_quickban_{target_id}_1'),
        types.InlineKeyboardButton("⛔ Бан 7 дней", callback_data=f'admin_quickban_{target_id}_7'),
        types.InlineKeyboardButton("⛔ Бан навсегда", callback_data=f'admin_quickban_{target_id}_perm')
    )
    markup.add(types.InlineKeyboardButton("💬 Написать пользователю", callback_data=f'admin_quickmsg_{target_id}'))
    nick = get_user_nickname(target_id)
    if nick:
        markup.add(types.InlineKeyboardButton("🗑 Удалить ник", callback_data=f'admin_quickremove_{target_id}'))
    banned, _, _, _ = is_banned(target_id)
    if banned:
        markup.add(types.InlineKeyboardButton("✅ Разбан", callback_data=f'admin_quickunban_{target_id}'))
    markup.add(types.InlineKeyboardButton("⬅️ Назад к списку", callback_data='admin_users_tab'))
    return markup

def get_admin_stats_keyboard():
    users_count = len(get_all_users())
    bans_count = len(get_all_bans())
    text = f"📊 Статистика:\n\n👥 Пользователей с ником: {users_count}\n🚫 Забанено: {bans_count}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data='admin_back_to_main'))
    return text, markup

def get_admin_info_keyboard():
    info_text = (
        "ℹ️ <b>ИНФОРМАЦИЯ О БОТЕ</b>\n\n"
        "<b>👤 Команды для пользователей:</b>\n"
        "/start — Главное меню\n"
        "📝 Создать псевдоним — через кнопку\n"
        "✏️ Удалить псевдоним — через кнопку (заявка)\n"
        "💻 Личный кабинет — просмотр статистики\n"
        "✍️ Написать администраторам — отправить сообщение\n"
        "🏴‍☠️ Анонимный шепотом вещает.. — отправить пост\n"
        "❓ FAQ — ответы на вопросы\n\n"
        "<b>🔧 Команды для администраторов:</b>\n"
        "/ban &lt;id&gt; [дни] [причина] — забанить пользователя\n"
        "/unban &lt;id&gt; — разбанить\n"
        "/remove_nick &lt;id&gt; — удалить ник\n"
        "/msg_user &lt;id&gt; &lt;текст&gt; — написать пользователю\n"
        "/broadcast &lt;текст&gt; — рассылка всем пользователям\n"
        "➕ Админ-панель с кнопками управления\n\n"
        "<b>👑 Команды только для владельца:</b>\n"
        "/setadmin &lt;id&gt; &lt;+/-&gt; — назначить/снять админа\n"
        "/stopbot — выключить бота\n"
    )
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data='admin_back_to_main'))
    return info_text, markup

def get_nickname_request_keyboard(request_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("✅ Удалить ник", callback_data=f'approve_delete_{request_id}'),
        types.InlineKeyboardButton("❌ Отклонить", callback_data=f'reject_delete_{request_id}')
    )
    return markup

def get_cancel_state_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data='cancel_state'))
    return markup

# ========== ОБРАБОТЧИКИ КОМАНД ==========
@bot.message_handler(commands=['start'])
def start(message):
    user_id = message.from_user.id
    banned, *_ = is_banned(user_id)
    if banned:
        return
    user = message.from_user
    username = f"@{user.username}" if user.username else "без юзернейма"
    if user_id in ADMIN_IDS:
        bot.send_message(message.chat.id,
            f"👋 Привет, {username}\n\n<b>🔐 Ты администратор</b>\nтебе доступны все функции бота.\n\nВыберите действие из меню ниже:",
            parse_mode='HTML', reply_markup=admin_main_keyboard())
        return
    if check_subscription(user_id):
        nick = get_user_nickname(user_id)
        if nick:
            bot.send_message(message.chat.id,
                f"👋 Привет, {username}\n\n<b>Твой псевдоним:</b> {nick}\nТеперь ты можешь отправлять сообщения администраторам.",
                parse_mode='HTML', reply_markup=user_main_keyboard())
        else:
            bot.send_message(message.chat.id,
                "👋 Привет, Ты подписан на канал.\n\nТеперь нужно создать уникальный псевдоним.\nНажми на кнопку \"📝 Создать псевдоним\" ниже.",
                reply_markup=no_nick_keyboard())
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔗 Вступить в канал", url=INVITE_LINK))
        markup.add(types.InlineKeyboardButton("✅ Проверить подписку", callback_data='check'))
        bot.send_message(message.chat.id, '❌ Ты не подписан на канал! Подпишись и нажми кнопку проверки.', reply_markup=markup)

# ========== CALLBACK ОБРАБОТЧИКИ ==========
@bot.callback_query_handler(func=lambda call: call.data == 'check')
def check_callback(call):
    user_id = call.from_user.id
    banned, *_ = is_banned(user_id)
    if banned:
        bot.edit_message_text("❌ Вы забанены. Доступ запрещён.", call.message.chat.id, call.message.message_id)
        return
    if check_subscription(user_id):
        bot.delete_message(call.message.chat.id, call.message.message_id)
        nick = get_user_nickname(user_id)
        username = f"@{call.from_user.username}" if call.from_user.username else "без юзернейма"
        if nick:
            bot.send_message(call.message.chat.id,
                f"👋 Привет, {username}\n\n<b>Твой псевдоним:</b> {nick}",
                parse_mode='HTML', reply_markup=user_main_keyboard())
        else:
            bot.send_message(call.message.chat.id,
                "👋 Привет!\n\nПодписка подтверждена.\nТеперь создай псевдоним, нажав на кнопку «📝 Создать псевдоним».",
                reply_markup=no_nick_keyboard())
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔗 Вступить в канал", url=INVITE_LINK))
        markup.add(types.InlineKeyboardButton("✅ Проверить подписку", callback_data='check'))
        bot.edit_message_text('❌ Ты всё ещё не подписан! Подпишись и нажми кнопку.',
                              call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'check_subscription_from_command')
def check_subscription_from_command_callback(call):
    user_id = call.from_user.id
    banned, *_ = is_banned(user_id)
    if banned:
        bot.edit_message_text("❌ Вы забанены. Доступ запрещён.", call.message.chat.id, call.message.message_id)
        return
    if check_subscription(user_id):
        bot.edit_message_text("✅ Подписка подтверждена! Теперь вам доступен весь функционал бота.\n\nИспользуйте кнопки меню для навигации.",
                              call.message.chat.id, call.message.message_id)
        nick = get_user_nickname(user_id)
        if nick:
            bot.send_message(call.message.chat.id, "Выберите действие:", reply_markup=user_main_keyboard())
        else:
            bot.send_message(call.message.chat.id, "Создайте псевдоним:", reply_markup=no_nick_keyboard())
    else:
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔗 Вступить в канал", url=INVITE_LINK))
        markup.add(types.InlineKeyboardButton("✅ Проверить подписку", callback_data='check_subscription_from_command'))
        bot.edit_message_text("❌ Вы всё ещё не подписаны на канал!\n\nПодпишитесь и нажмите кнопку проверки.",
                              call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == 'create_nick_from_require')
def create_nick_from_require_callback(call):
    user_id = call.from_user.id
    banned, *_ = is_banned(user_id)
    if banned:
        bot.answer_callback_query(call.id, "Вы забанены")
        return
    if user_id not in ADMIN_IDS and not check_subscription(user_id):
        bot.answer_callback_query(call.id, "❌ Вы не подписаны на канал!")
        return
    set_state(user_id, 'awaiting_nickname', True)
    bot.edit_message_text("Введите ваш новый псевдоним (от 4 до 12 символов, только латиница, цифры и _):", 
                          call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == 'cancel_state')
def cancel_state_callback(call):
    user_id = call.from_user.id
    clear_state(user_id)
    bot.edit_message_text("❌ Действие отменено.", call.message.chat.id, call.message.message_id)
    nick = get_user_nickname(user_id)
    if nick:
        bot.send_message(call.message.chat.id, "Вы в главном меню.", reply_markup=user_main_keyboard())
    else:
        bot.send_message(call.message.chat.id, "Вы в главном меню.", reply_markup=no_nick_keyboard())

# ========== ОБРАБОТЧИКИ КНОПОК МЕНЮ ==========
@bot.message_handler(func=lambda m: m.text == "📝 Создать псевдоним")
@require_subscription_and_antispam(command_name="📝 Создать псевдоним")
def create_nickname_prompt(message):
    user_id = message.from_user.id
    set_state(user_id, 'awaiting_nickname', True)
    bot.send_message(message.chat.id, "Введите ваш новый псевдоним (от 4 до 12 символов, только латиница, цифры и _):")

@bot.message_handler(func=lambda m: m.text == "✏️ Удалить псевдоним")
@require_subscription_and_antispam(command_name="✏️ Удалить псевдоним")
def delete_nickname_prompt(message):
    user_id = message.from_user.id
    current_nick = get_user_nickname(user_id)
    if not current_nick:
        bot.send_message(message.chat.id, "❌ У вас ещё нет псевдонима. Сначала создайте его через кнопку «📝 Создать псевдоним».")
        return
    can_request, remaining = can_request_delete(user_id)
    if not can_request:
        bot.send_message(message.chat.id, f"❌ Вы уже подавали заявку на удаление, и она была отклонена. Следующую заявку можно отправить через {remaining}.")
        return
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM nickname_requests WHERE user_id = ? AND status != "pending"', (user_id,))
        conn.commit()
        conn.close()
    set_state(user_id, 'awaiting_delete_reason', True)
    bot.send_message(message.chat.id,
        f"📝 Вы хотите удалить свой псевдоним {current_nick}.\n\n"
        "Напишите причину удаления (одним сообщением).\n"
        "Администраторы рассмотрят вашу заявку в течение 24 часов.\n"
        "После одобрения вы сможете создать новый псевдоним.",
        reply_markup=get_cancel_state_keyboard())

@bot.message_handler(func=lambda m: m.text == "💻 Личный кабинет")
@require_subscription_and_antispam(command_name="💻 Личный кабинет")
def personal_cabinet(message):
    user_id = message.from_user.id
    user = message.from_user
    username = f"@{user.username}" if user.username else "не указан"
    nick = get_user_nickname(user_id) or "не установлен"
    reputation = get_user_reputation(user_id)
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM anonymous_posts WHERE user_id = ? AND status = "approved"', (user_id,))
        posts_count = cursor.fetchone()[0]
        cursor.execute('SELECT SUM(likes), SUM(dislikes) FROM anonymous_posts WHERE user_id = ? AND status = "approved"', (user_id,))
        row = cursor.fetchone()
        total_likes, total_dislikes = row if row else (0, 0)
        conn.close()
    text = (
        f"<b>💻 Личный кабинет</b>\n\n"
        f"👤 ID: <code>{user_id}</code>\n"
        f"📛 Псевдоним: {nick}\n"
        f"👥 Username: {username}\n"
        f"📅 Имя: {user.full_name}\n"
        f"🥇 Репутация: {reputation}\n"
        f"📊 Постов: {posts_count} | 👍 {total_likes or 0} | 👎 {total_dislikes or 0}\n"
    )
    banned, ban_reason, banned_by, ban_time = is_banned(user_id)
    if banned:
        ban_until = get_ban_until(user_id)
        if ban_until == -1:
            ban_info = "\n🚫 БАН: навсегда"
        else:
            remaining = max(0, ban_until - int(time.time()))
            days = remaining // (24 * 3600)
            hours = (remaining % (24 * 3600)) // 3600
            ban_info = f"\n🚫 БАН: {days} дн. {hours} ч."
        ban_info += f"\n📝 Причина: {ban_reason}"
        text += ban_info
    bot.send_message(message.chat.id, text, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "❓ FAQ")
@require_subscription_and_antispam(command_name="❓ FAQ")
def faq(message):
    faq_text = (
        "❓ <b>Часто задаваемые вопросы</b>\n\n"
        "1. Как создать псевдоним?\n"
        "   Нажмите кнопку «📝 Создать псевдоним» и следуйте инструкциям.\n\n"
        "2. Как написать администраторам?\n"
        "   Нажмите «✍️ Написать администраторам» и отправьте сообщение.\n\n"
        "3. Как удалить псевдоним?\n"
        "   Нажмите «✏️ Удалить псевдоним», укажите причину — заявка уйдёт админам.\n\n"
        "4. Как отправить анонимное сообщение?\n"
        "   Нажмите «🏴‍☠️ Анонимный шепотом вещает..» и отправьте до 4 фото или 1 видео до 40 секунд.\n\n"
        "5. Что такое репутация?\n"
        "   Это сумма лайков и дизлайков под вашими анонимными постами в канале."
    )
    bot.send_message(message.chat.id, faq_text, parse_mode='HTML')

@bot.message_handler(func=lambda m: m.text == "✍️ Написать администраторам")
@require_subscription_and_antispam(command_name="✍️ Написать администраторам")
def contact_admin_prompt(message):
    user_id = message.from_user.id
    nick = get_user_nickname(user_id)
    if not nick:
        bot.send_message(message.chat.id, 
            "❌ Сначала создайте псевдоним через кнопку «📝 Создать псевдоним».", 
            reply_markup=require_nick_keyboard())
        return
    set_state(user_id, 'awaiting_message', True)
    bot.send_message(message.chat.id,
        "📝 Отправьте ваше сообщение.\n\n"
        "<b>Разрешено отправлять:</b>\n"
        "• Текст\n"
        "• До 4 фотографий (можно с текстом)\n"
        "• 1 видео до 40 секунд\n"
        "• Аудио / голосовое сообщение\n\n"
        "<i>Всё остальное будет отклонено.</i>",
        parse_mode='HTML', reply_markup=get_cancel_state_keyboard())

@bot.message_handler(func=lambda m: m.text == "🏴‍☠️ Анонимный шепотом вещает..")
@require_subscription_and_antispam(command_name="🏴‍☠️ Анонимный шепотом вещает..")
def anonymous_post_prompt(message):
    user_id = message.from_user.id
    nick = get_user_nickname(user_id)
    if not nick:
        bot.send_message(message.chat.id, 
            "❌ Сначала создайте псевдоним через кнопку «📝 Создать псевдоним».", 
            reply_markup=require_nick_keyboard())
        return
    as_admin = (user_id in ADMIN_IDS)
    set_state(user_id, 'awaiting_anonymous_post', {'as_admin': as_admin})
    bot.send_message(message.chat.id,
        "📝 <b>Отправьте ваш анонимный пост.</b>\n\n"
        "<b>Разрешено отправлять:</b>\n"
        "• Текст\n"
        "• До 4 фотографий (можно с текстом)\n"
        "• 1 видео до 40 секунд\n\n"
        "Пост будет отправлен на модерацию администраторам.",
        parse_mode='HTML', reply_markup=get_cancel_state_keyboard())

@bot.message_handler(func=lambda m: m.text == "🔧 Админ-панель")
def admin_panel(message):
    user_id = message.from_user.id
    banned, *_ = is_banned(user_id)
    if banned:
        return
    if user_id not in ADMIN_IDS:
        bot.send_message(message.chat.id, "У вас нет прав для этого действия.")
        return
    bot.send_message(message.chat.id, "🔧 Админ-панель:", reply_markup=get_admin_main_inline_keyboard())

# ========== ОБРАБОТКА ВСЕХ СООБЩЕНИЙ ==========
@bot.message_handler(content_types=['text', 'photo', 'video', 'animation', 'audio', 'voice', 'video_note', 'document', 'sticker'])
def handle_all_messages(message):
    user_id = message.from_user.id
    banned, *_ = is_banned(user_id)
    if banned:
        return

    if user_id not in ADMIN_IDS and not check_subscription(user_id):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("🔗 Вступить в канал", url=INVITE_LINK))
        markup.add(types.InlineKeyboardButton("✅ Проверить подписку", callback_data='check_subscription_from_command'))
        bot.send_message(message.chat.id,
            "❌ Вы не подписаны на канал! Подпишитесь для использования бота.",
            reply_markup=markup)
        return

    if message.content_type == 'text' and message.text and message.text.startswith('/'):
        handle_admin_commands(message)
        return

    if get_state(user_id, 'awaiting_nickname'):
        if message.content_type == 'text':
            process_nickname_input(message)
        else:
            bot.send_message(message.chat.id, "❌ Отправьте текст псевдонима.")
    elif get_state(user_id, 'awaiting_delete_reason'):
        if message.content_type == 'text':
            process_delete_request(message)
        else:
            bot.send_message(message.chat.id, "❌ Отправьте причину текстом.")
    elif get_state(user_id, 'awaiting_message'):
        forward_user_message(message)
    elif get_state(user_id, 'awaiting_anonymous_post'):
        state_data = get_state(user_id, 'awaiting_anonymous_post')
        as_admin = state_data.get('as_admin', False) if isinstance(state_data, dict) else False
        process_anonymous_post(message, as_admin=as_admin)
    elif user_id in ADMIN_IDS:
        if get_state(user_id, 'awaiting_broadcast'):
            if message.content_type == 'text':
                if message.text.strip().lower() in ['отмена', 'cancel', '/cancel']:
                    bot.send_message(message.chat.id, "❌ Операция отменена.", reply_markup=admin_main_keyboard())
                    clear_state(user_id, 'awaiting_broadcast')
                    return
                process_broadcast_input(message)
            else:
                bot.send_message(message.chat.id, "❌ Отправьте текст рассылки.")
        elif get_state(user_id, 'awaiting_ban_target'):
            if message.content_type == 'text':
                if message.text.strip().lower() in ['отмена', 'cancel', '/cancel']:
                    bot.send_message(message.chat.id, "❌ Операция отменена.", reply_markup=admin_main_keyboard())
                    clear_state(user_id, 'awaiting_ban_target')
                    return
                process_ban_target_input(message)
            else:
                bot.send_message(message.chat.id, "❌ Отправьте ID и причину текстом.")
        elif get_state(user_id, 'awaiting_unban_target'):
            if message.content_type == 'text':
                if message.text.strip().lower() in ['отмена', 'cancel', '/cancel']:
                    bot.send_message(message.chat.id, "❌ Операция отменена.", reply_markup=admin_main_keyboard())
                    clear_state(user_id, 'awaiting_unban_target')
                    return
                process_unban_target_input(message)
            else:
                bot.send_message(message.chat.id, "❌ Отправьте ID текстом.")
        elif get_state(user_id, 'awaiting_remove_nick_target'):
            if message.content_type == 'text':
                if message.text.strip().lower() in ['отмена', 'cancel', '/cancel']:
                    bot.send_message(message.chat.id, "❌ Операция отменена.", reply_markup=admin_main_keyboard())
                    clear_state(user_id, 'awaiting_remove_nick_target')
                    return
                process_remove_nick_target_input(message)
            else:
                bot.send_message(message.chat.id, "❌ Отправьте ID текстом.")
        elif get_state(user_id, 'awaiting_msg_user_target'):
            if message.content_type == 'text':
                if message.text.strip().lower() in ['отмена', 'cancel', '/cancel']:
                    bot.send_message(message.chat.id, "❌ Операция отменена.", reply_markup=admin_main_keyboard())
                    clear_state(user_id, 'awaiting_msg_user_target')
                    clear_state(user_id, 'quick_msg_target')
                    return
                process_msg_user_target_input(message)
            else:
                bot.send_message(message.chat.id, "❌ Отправьте ID и сообщение текстом.")

# ========== ОБРАБОТКА ВВОДА ==========
def process_nickname_input(message):
    user_id = message.from_user.id
    base_nick = message.text.strip()
    valid, msg = validate_nickname_base(base_nick)
    if not valid:
        bot.send_message(message.chat.id, f"❌ {msg}\nПопробуйте ещё раз.")
        return
    full_nick = '#' + base_nick
    can_change, next_time = can_change_nickname(user_id)
    if not can_change:
        bot.send_message(message.chat.id, f"❌ Ты уже менял ник. Следующая смена доступна: {next_time}")
        clear_state(user_id, 'awaiting_nickname')
        return
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM users WHERE nickname = ?', (full_nick,))
        exists = cursor.fetchone()
        conn.close()
    if exists:
        bot.send_message(message.chat.id, "❌ Этот псевдоним уже занят. Придумай другой.")
        return
    old_nick = get_user_nickname(user_id)
    if old_nick:
        update_user_nickname(user_id, full_nick)
        bot.send_message(message.chat.id, f"✅ Твой псевдоним изменён на {full_nick}", reply_markup=user_main_keyboard())
        notify_group(f"<blockquote>От: {message.from_user.full_name} (@{message.from_user.username or 'нет'})\nID: {user_id}\nСменил псевдоним:\n{old_nick} → {full_nick}</blockquote>")
    else:
        success = set_user_nickname(user_id, full_nick)
        if success:
            bot.send_message(message.chat.id, f"✅ Псевдоним {full_nick} успешно установлен!", reply_markup=user_main_keyboard())
            notify_group(f"<blockquote>От: {message.from_user.full_name} (@{message.from_user.username or 'нет'})\nID: {user_id}\nУстановлен новый псевдоним:\n{full_nick}</blockquote>")
        else:
            bot.send_message(message.chat.id, "❌ Не удалось сохранить псевдоним. Попробуй другой.")
    clear_state(user_id, 'awaiting_nickname')

def process_delete_request(message):
    user_id = message.from_user.id
    reason = message.text.strip()
    if len(reason) < 3:
        bot.send_message(message.chat.id, "❌ Причина должна содержать хотя бы 3 символа.")
        return
    current_nick = get_user_nickname(user_id)
    if not current_nick:
        bot.send_message(message.chat.id, "❌ У вас нет псевдонима.")
        clear_state(user_id, 'awaiting_delete_reason')
        return
    can_request, remaining = can_request_delete(user_id)
    if not can_request:
        bot.send_message(message.chat.id, f"❌ Следующую заявку можно отправить через {remaining}.")
        clear_state(user_id, 'awaiting_delete_reason')
        return
    with db_lock:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM nickname_requests WHERE user_id = ? AND status = "pending"', (user_id,))
        exists = cursor.fetchone()
        conn.close()
    if exists:
        bot.send_message(message.chat.id, "❌ У вас уже есть активная заявка.")
        clear_state(user_id, 'awaiting_delete_reason')
        return
    request_id = create_delete_request(user_id, reason)
    user = message.from_user
    username = f"@{user.username}" if user.username else "нет username"
    admin_text = (
        f"<b>📝 Заявка на удаление псевдонима</b>\n\n"
        f"👤 {user.full_name} ({username})\n"
        f"🆔 <code>{user_id}</code>\n"
        f"📛 Текущий: {current_nick}\n"
        f"📋 Причина: {reason}\n\n"
        f"⏳ Действительна 24 часа."
    )
    bot.send_message(GROUP_ID, admin_text, parse_mode='HTML', reply_markup=get_nickname_request_keyboard(request_id))
    bot.send_message(message.chat.id, "✅ Заявка отправлена администраторам.")
    clear_state(user_id, 'awaiting_delete_reason')

def forward_user_message(message):
    user_id = message.from_user.id
    if not check_subscription(user_id):
        bot.send_message(message.chat.id, "❌ Вы не подписаны на канал.")
        clear_state(user_id, 'awaiting_message')
        return
    nick = get_user_nickname(user_id)
    if not nick:
        bot.send_message(message.chat.id, "❌ У вас нет псевдонима.")
        clear_state(user_id, 'awaiting_message')
        return

    user = message.from_user
    username = f"@{user.username}" if user.username else "нет username"
    user_info = f"От: {user.full_name} ({username})\nID: {user.id}\nПсевдоним: {nick}"

    def make_quote(text):
        if not text:
            return ""
        return f"<blockquote>{escape_html(text)}</blockquote>"

    content_type = message.content_type

    if content_type == 'text':
        bot.send_message(GROUP_ID, f"{user_info}\n\n{make_quote(message.text)}", parse_mode='HTML')
        bot.send_message(message.chat.id, "✅ Сообщение отправлено администраторам.")
        clear_state(user_id, 'awaiting_message')
        return

    elif content_type == 'photo':
        media_group_id = message.media_group_id
        if media_group_id:
            if media_group_id not in user_media_groups:
                user_media_groups[media_group_id] = {
                    'photos': [],
                    'caption': message.caption,
                    'user_info': user_info,
                    'chat_id': message.chat.id,
                    'user_id': user_id,
                    'type': 'message'
                }
            user_media_groups[media_group_id]['photos'].append(message.photo[-1].file_id)
            def process_media_group(mg_id):
                time.sleep(1)
                if mg_id in user_media_groups:
                    data = user_media_groups[mg_id]
                    photos = data['photos']
                    caption = data['caption'] or ""
                    u_info = data['user_info']
                    chat_id = data['chat_id']
                    u_id = data['user_id']
                    if len(photos) > 4:
                        bot.send_message(chat_id, "❌ Разрешено максимум 4 фотографии.")
                        del user_media_groups[mg_id]
                        clear_state(u_id, 'awaiting_message')
                        return
                    quoted = make_quote(caption)
                    if len(photos) == 1:
                        bot.send_photo(GROUP_ID, photos[0], caption=f"{u_info}\n\n{quoted}", parse_mode='HTML')
                    else:
                        media_list = []
                        for i, fid in enumerate(photos):
                            if i == 0:
                                media_list.append(types.InputMediaPhoto(fid, caption=f"{u_info}\n\n{quoted}" if caption else u_info, parse_mode='HTML'))
                            else:
                                media_list.append(types.InputMediaPhoto(fid))
                        bot.send_media_group(GROUP_ID, media_list)
                    bot.send_message(chat_id, "✅ Сообщение отправлено администраторам.")
                    del user_media_groups[mg_id]
                    clear_state(u_id, 'awaiting_message')
            if len(user_media_groups[media_group_id]['photos']) == 1:
                threading.Thread(target=process_media_group, args=(media_group_id,), daemon=True).start()
            return
        else:
            caption = message.caption or ""
            bot.send_photo(GROUP_ID, message.photo[-1].file_id, caption=f"{user_info}\n\n{make_quote(caption)}", parse_mode='HTML')
            bot.send_message(message.chat.id, "✅ Сообщение отправлено администраторам.")
            clear_state(user_id, 'awaiting_message')
            return

    elif content_type in ['video', 'animation', 'video_note']:
        duration = None
        file_id = None
        if content_type == 'video':
            duration = message.video.duration
            file_id = message.video.file_id
        elif content_type == 'animation':
            duration = message.animation.duration
            file_id = message.animation.file_id
        elif content_type == 'video_note':
            duration = message.video_note.duration
            file_id = message.video_note.file_id
        if duration and duration > 40:
            bot.send_message(message.chat.id, "❌ Видео должно быть не длиннее 40 секунд.")
            clear_state(user_id, 'awaiting_message')
            return
        caption = message.caption or ""
        quoted = make_quote(caption)
        if content_type == 'video':
            bot.send_video(GROUP_ID, file_id, caption=f"{user_info}\n\n{quoted}", parse_mode='HTML')
        elif content_type == 'animation':
            bot.send_animation(GROUP_ID, file_id, caption=f"{user_info}\n\n{quoted}", parse_mode='HTML')
        elif content_type == 'video_note':
            bot.send_video_note(GROUP_ID, file_id)
            if caption:
                bot.send_message(GROUP_ID, f"{user_info}\n\n{quoted}", parse_mode='HTML')
            else:
                bot.send_message(GROUP_ID, user_info)
        bot.send_message(message.chat.id, "✅ Сообщение отправлено администраторам.")
        clear_state(user_id, 'awaiting_message')
        return

    elif content_type in ['audio', 'voice']:
        caption = message.caption or ""
        quoted = make_quote(caption)
        if content_type == 'audio':
            bot.send_audio(GROUP_ID, message.audio.file_id, caption=f"{user_info}\n\n{quoted}", parse_mode='HTML')
        elif content_type == 'voice':
            bot.send_voice(GROUP_ID, message.voice.file_id)
            if caption:
                bot.send_message(GROUP_ID, f"{user_info}\n\n{quoted}", parse_mode='HTML')
            else:
                bot.send_message(GROUP_ID, user_info)
        bot.send_message(message.chat.id, "✅ Сообщение отправлено администраторам.")
        clear_state(user_id, 'awaiting_message')
        return

    else:
        bot.send_message(message.chat.id,
            "❌ <b>Разрешено отправлять только:</b>\n"
            "• Текст\n"
            "• До 4 фотографий\n"
            "• 1 видео до 40 секунд\n"
            "• Аудио / голосовое сообщение\n\n"
            "<i>Попробуйте ещё раз.</i>",
            parse_mode='HTML')
        return

def process_anonymous_post(message, as_admin=False):
    user_id = message.from_user.id
    nick = get_user_nickname(user_id)
    if not nick:
        bot.send_message(message.chat.id, "❌ У вас нет псевдонима.")
        clear_state(user_id, 'awaiting_anonymous_post')
        return

    user = message.from_user
    username = f"@{user.username}" if user.username else "нет username"

    if as_admin:
        user_info = (
            f"👑 <b>ПОСТ ОТ АДМИНИСТРАЦИИ</b>\n"
            f"👤 {user.full_name} (@{username})  │  🆔 <code>{user_id}</code>\n"
            f"📛 {nick}\n"
            f"<code>{'─' * 20}</code>"
        )
    else:
        user_info = (
            f"📢 <b>АНОНИМНЫЙ ШЕПОТ НА МОДЕРАЦИИ</b>\n"
            f"👤 {user.full_name} (@{username})  │  🆔 <code>{user_id}</code>\n"
            f"📛 {nick}\n"
            f"<code>{'─' * 20}</code>"
        )

    def make_quote(text):
        if not text:
            return ""
        return f"<blockquote>{escape_html(text)}</blockquote>"

    content_type = message.content_type

    if content_type == 'text':
        post_id = save_anonymous_post(user_id, nick, None, '', message.text, as_admin, media_type='text')
        quoted = make_quote(message.text)
        bot.send_message(GROUP_ID, f"{user_info}\n\n{quoted}", parse_mode='HTML', reply_markup=get_post_moderation_keyboard(post_id))
        bot.send_message(message.chat.id, "✅ Ваш пост отправлен на модерацию.")
        clear_state(user_id, 'awaiting_anonymous_post')
        return

    elif content_type == 'photo':
        media_group_id = message.media_group_id
        if media_group_id:
            if media_group_id not in user_media_groups:
                user_media_groups[media_group_id] = {
                    'photos': [],
                    'caption': message.caption,
                    'user_info': user_info,
                    'chat_id': message.chat.id,
                    'user_id': user_id,
                    'nick': nick,
                    'as_admin': as_admin,
                    'type': 'anonymous_post'
                }
            user_media_groups[media_group_id]['photos'].append(message.photo[-1].file_id)
            def process_media_group(mg_id):
                time.sleep(1)
                if mg_id in user_media_groups:
                    data = user_media_groups[mg_id]
                    photos = data['photos']
                    caption = data['caption'] or ""
                    u_info = data['user_info']
                    chat_id = data['chat_id']
                    u_id = data['user_id']
                    nick = data['nick']
                    as_admin = data['as_admin']
                    if len(photos) > 4:
                        bot.send_message(chat_id, "❌ Разрешено максимум 4 фотографии.")
                        del user_media_groups[mg_id]
                        clear_state(u_id, 'awaiting_anonymous_post')
                        return
                    post_id = save_anonymous_post(u_id, nick, mg_id, photos, caption, as_admin, media_type='photo')
                    quoted = make_quote(caption)
                    if len(photos) == 1:
                        bot.send_photo(GROUP_ID, photos[0], caption=f"{u_info}\n\n{quoted}", parse_mode='HTML', reply_markup=get_post_moderation_keyboard(post_id))
                    else:
                        media_list = []
                        for i, fid in enumerate(photos):
                            if i == 0:
                                media_list.append(types.InputMediaPhoto(fid, caption=f"{u_info}\n\n{quoted}" if caption else u_info, parse_mode='HTML'))
                            else:
                                media_list.append(types.InputMediaPhoto(fid))
                        msgs = bot.send_media_group(GROUP_ID, media_list)
                        if msgs:
                            bot.send_message(GROUP_ID, "⏫ Пост выше", reply_markup=get_post_moderation_keyboard(post_id))
                    bot.send_message(chat_id, "✅ Ваш пост отправлен на модерацию.")
                    del user_media_groups[mg_id]
                    clear_state(u_id, 'awaiting_anonymous_post')
            if len(user_media_groups[media_group_id]['photos']) == 1:
                threading.Thread(target=process_media_group, args=(media_group_id,), daemon=True).start()
            return
        else:
            post_id = save_anonymous_post(user_id, nick, None, [message.photo[-1].file_id], message.caption or "", as_admin, media_type='photo')
            caption = message.caption or ""
            quoted = make_quote(caption)
            bot.send_photo(GROUP_ID, message.photo[-1].file_id, caption=f"{user_info}\n\n{quoted}", parse_mode='HTML', reply_markup=get_post_moderation_keyboard(post_id))
            bot.send_message(message.chat.id, "✅ Ваш пост отправлен на модерацию.")
            clear_state(user_id, 'awaiting_anonymous_post')
            return

    elif content_type in ['video', 'animation']:
        duration = None
        file_id = None
        if content_type == 'video':
            duration = message.video.duration
            file_id = message.video.file_id
        elif content_type == 'animation':
            duration = message.animation.duration
            file_id = message.animation.file_id
        if duration and duration > 40:
            bot.send_message(message.chat.id, "❌ Видео должно быть не длиннее 40 секунд.")
            clear_state(user_id, 'awaiting_anonymous_post')
            return
        post_id = save_anonymous_post(user_id, nick, None, [file_id], message.caption or "", as_admin, media_type='video')
        caption = message.caption or ""
        quoted = make_quote(caption)
        if content_type == 'video':
            bot.send_video(GROUP_ID, file_id, caption=f"{user_info}\n\n{quoted}", parse_mode='HTML', reply_markup=get_post_moderation_keyboard(post_id))
        else:
            bot.send_animation(GROUP_ID, file_id, caption=f"{user_info}\n\n{quoted}", parse_mode='HTML', reply_markup=get_post_moderation_keyboard(post_id))
        bot.send_message(message.chat.id, "✅ Ваш пост отправлен на модерацию.")
        clear_state(user_id, 'awaiting_anonymous_post')
        return

    else:
        bot.send_message(message.chat.id,
            "❌ <b>Разрешено отправлять только:</b>\n"
            "• Текст\n"
            "• До 4 фотографий\n"
            "• 1 видео до 40 секунд\n\n"
            "<i>Попробуйте ещё раз.</i>",
            parse_mode='HTML')
        return

# ========== АДМИНСКИЕ КОМАНДЫ ==========
def handle_admin_commands(message):
    admin_id = message.from_user.id
    if admin_id not in ADMIN_IDS:
        return
    text = message.text.strip()
    parts = text.split()
    cmd = parts[0].lower()

    if cmd == '/setadmin':
        if admin_id != UNTOUCHABLE_USER_ID:
            bot.send_message(message.chat.id, "❌ Только владелец может управлять администраторами.")
            return
        if len(parts) < 3:
            bot.send_message(message.chat.id, "Использование: /setadmin <user_id> <+ или ->")
            return
        try:
            target_id = int(parts[1])
            action = parts[2]
        except:
            bot.send_message(message.chat.id, "❌ Неверный формат. Пример: /setadmin 123456789 +")
            return
        if target_id == UNTOUCHABLE_USER_ID:
            bot.send_message(message.chat.id, "❌ Нельзя изменить статус владельца.")
            return
        if action == '+':
            if target_id in ADMIN_IDS:
                bot.send_message(message.chat.id, f"❌ Пользователь {target_id} уже администратор.")
                return
            ADMIN_IDS.append(target_id)
            log_admin_action(admin_id, "ADD_ADMIN", target_id, "Добавлен администратор")
            bot.send_message(message.chat.id, f"✅ Пользователь {target_id} теперь администратор.")
            notify_group(f"<blockquote>👑 Владелец добавил администратора: {target_id}</blockquote>")
            try:
                bot.send_message(target_id, "🎉 Вы назначены администратором! Используйте /start.")
            except: pass
        elif action == '-':
            if target_id not in ADMIN_IDS:
                bot.send_message(message.chat.id, f"❌ Пользователь {target_id} не администратор.")
                return
            ADMIN_IDS.remove(target_id)
            log_admin_action(admin_id, "REMOVE_ADMIN", target_id, "Снят администратор")
            bot.send_message(message.chat.id, f"✅ Пользователь {target_id} больше не администратор.")
            notify_group(f"<blockquote>👑 Владелец снял администратора: {target_id}</blockquote>")
            try:
                bot.send_message(target_id, "⚠️ Вы сняты с должности администратора.")
            except: pass
        else:
            bot.send_message(message.chat.id, "❌ Используйте + для добавления или - для удаления.")
        return

    if cmd == '/broadcast':
        if admin_id not in ADMIN_IDS:
            bot.send_message(message.chat.id, "❌ Нет прав.")
            return
        broadcast_text = text[len('/broadcast '):].strip()
        if not broadcast_text:
            bot.send_message(message.chat.id, "Использование: /broadcast <текст сообщения>")
            return
        threading.Thread(target=do_broadcast, args=(admin_id, broadcast_text, message.chat.id)).start()
        bot.send_message(message.chat.id, "⏳ Рассылка начата. Это может занять некоторое время.")
        return

    if cmd == '/stopbot':
        if admin_id != UNTOUCHABLE_USER_ID:
            bot.send_message(message.chat.id, "❌ Только владелец может выключить бота.")
            return
        bot.send_message(message.chat.id, "🛑 Выключаюсь...")
        bot.stop_polling()
        exit()
        return

    if cmd == '/ban':
        if len(parts) < 2:
            bot.send_message(message.chat.id, "/ban <user_id> [дни] [причина]")
            return
        try:
            target_id = int(parts[1])
            days = None
            reason_start = 2
            if len(parts) >= 3 and parts[2].isdigit():
                days = int(parts[2])
                reason_start = 3
            reason = " ".join(parts[reason_start:]) if len(parts) > reason_start else "Не указана"
        except:
            bot.send_message(message.chat.id, "Неверный формат.")
            return
        if target_id == admin_id:
            bot.send_message(message.chat.id, "❌ Нельзя забанить самого себя.")
            return
        is_violation, violator_id = check_untouchable(target_id, admin_id, "бан")
        if is_violation:
            bot.send_message(message.chat.id, "❌ Вы попытались забанить неприкасаемого. Вы забанены навсегда!")
            return
        if target_id in ADMIN_IDS and admin_id != target_id:
            handle_admin_vs_admin_ban(admin_id, target_id, reason)
            bot.send_message(message.chat.id, "❌ Вы попытались забанить администратора. Вы оба забанены навсегда!")
            return
        success, error = ban_user(target_id, duration_days=days, reason=reason, banned_by=admin_id)
        if success:
            log_admin_action(admin_id, "BAN", target_id, f"дни:{days if days else 'навсегда'} причина:{reason}")
            bot.send_message(message.chat.id, f"✅ Пользователь {target_id} забанен.")
            notify_group(f"<blockquote>🚨 Админ {admin_id} забанил {target_id}\nПричина: {reason}</blockquote>")
        else:
            bot.send_message(message.chat.id, f"❌ {error}")

    elif cmd == '/unban':
        if len(parts) < 2:
            bot.send_message(message.chat.id, "/unban <user_id>")
            return
        try:
            target_id = int(parts[1])
        except:
            bot.send_message(message.chat.id, "Неверный ID")
            return
        is_violation, violator_id = check_untouchable(target_id, admin_id, "разбан")
        if is_violation:
            if violator_id == "owner_unban_attempt":
                bot.send_message(message.chat.id, "❌ Владелец не может быть забанен или разбанен.")
                return
            bot.send_message(message.chat.id, "❌ Вы попытались взаимодействовать с неприкасаемым. Вы забанены навсегда!")
            return
        unban_user(target_id)
        log_admin_action(admin_id, "UNBAN", target_id)
        bot.send_message(message.chat.id, f"✅ Бан с {target_id} снят.")
        notify_group(f"<blockquote>🚨 Админ {admin_id} разбанил {target_id}</blockquote>")

    elif cmd == '/remove_nick':
        if len(parts) < 2:
            bot.send_message(message.chat.id, "/remove_nick <user_id>")
            return
        try:
            target_id = int(parts[1])
        except:
            bot.send_message(message.chat.id, "Неверный ID")
            return
        is_violation, violator_id = check_untouchable(target_id, admin_id, "удаление ника")
        if is_violation:
            bot.send_message(message.chat.id, "❌ Вы попытались удалить ник неприкасаемого. Вы забанены навсегда!")
            return
        if remove_user_nickname(target_id):
            bot.send_message(message.chat.id, f"✅ Ник {target_id} удалён.")
            log_admin_action(admin_id, "REMOVE_NICK", target_id)
        else:
            bot.send_message(message.chat.id, "❌ Пользователь не найден.")

    elif cmd == '/msg_user':
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            bot.send_message(message.chat.id, "/msg_user <user_id> <текст>")
            return
        try:
            target_id = int(parts[1])
            msg_text = parts[2]
        except:
            bot.send_message(message.chat.id, "Неверный формат")
            return
        if target_id == UNTOUCHABLE_USER_ID and admin_id != UNTOUCHABLE_USER_ID:
            log_admin_action(admin_id, "MSG_UNTOUCHABLE", target_id, msg_text[:100])
            bot.send_message(message.chat.id, "⚠️ Сообщение неприкасаемому залогировано.")
        try:
            bot.send_message(target_id, f"📨 Сообщение от администратора:\n\n{msg_text}")
            bot.send_message(message.chat.id, f"✅ Отправлено пользователю {target_id}")
            log_admin_action(admin_id, "MSG_USER", target_id, msg_text[:100])
        except Exception as e:
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")

# ========== ОБРАБОТКА ВВОДА ДЛЯ АДМИНОВ ==========
def process_broadcast_input(message):
    admin_id = message.from_user.id
    broadcast_text = message.text.strip()
    if not broadcast_text:
        bot.send_message(message.chat.id, "❌ Текст не может быть пустым.")
        return
    threading.Thread(target=do_broadcast, args=(admin_id, broadcast_text, message.chat.id)).start()
    bot.send_message(message.chat.id, "⏳ Рассылка начата. Это может занять некоторое время.")
    clear_state(admin_id, 'awaiting_broadcast')

def do_broadcast(admin_id, text, reply_chat_id):
    users = get_all_users()
    success = 0
    for row in users:
        uid = row[0]
        try:
            bot.send_message(uid, f"📢 <b>Сообщение от администрации:</b>\n\n{text}", parse_mode='HTML')
            success += 1
            time.sleep(0.05)
        except:
            pass
    bot.send_message(reply_chat_id, f"✅ Рассылка завершена. Отправлено {success} из {len(users)} пользователей.")
    notify_group(f"<blockquote>📢 Администратор {admin_id} сделал рассылку: {text[:100]}...</blockquote>")

def process_ban_target_input(message):
    admin_id = message.from_user.id
    text = message.text.strip()
    parts = text.split(maxsplit=1)
    if not parts:
        bot.send_message(message.chat.id, "❌ Введите ID и причину.")
        return
    try:
        target_id = int(parts[0])
    except:
        bot.send_message(message.chat.id, "❌ ID должен быть числом.")
        return
    reason = parts[1] if len(parts) > 1 else "Не указана"

    if target_id == admin_id:
        bot.send_message(message.chat.id, "❌ Нельзя забанить самого себя.")
        clear_state(admin_id, 'awaiting_ban_target')
        return

    is_violation, violator_id = check_untouchable(target_id, admin_id, "бан")
    if is_violation:
        bot.send_message(message.chat.id, "❌ Попытка забанить неприкасаемого. Вы забанены навсегда!")
        clear_state(admin_id, 'awaiting_ban_target')
        return
    if target_id in ADMIN_IDS and admin_id != target_id:
        handle_admin_vs_admin_ban(admin_id, target_id, reason)
        bot.send_message(message.chat.id, "❌ Вы попытались забанить администратора. Вы оба забанены навсегда!")
        clear_state(admin_id, 'awaiting_ban_target')
        return

    banned, existing_reason, banned_by, ban_time = is_banned(target_id)
    duration = get_state(admin_id, 'ban_duration')
    is_perm = get_state(admin_id, 'ban_perm') or False

    if banned:
        if get_ban_until(target_id) == -1:
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("✅ Разбанить", callback_data=f'force_unban_{target_id}'))
            bot.send_message(message.chat.id, f"❌ Уже перманентный бан.", reply_markup=markup)
        else:
            remaining_days = get_remaining_ban_time(target_id) // (24*3600)
            markup = types.InlineKeyboardMarkup(row_width=2)
            markup.add(types.InlineKeyboardButton("➕ Добавить время", callback_data=f'add_ban_time_{target_id}'))
            markup.add(types.InlineKeyboardButton("✅ Разбанить", callback_data=f'force_unban_{target_id}'))
            bot.send_message(message.chat.id, f"⚠️ Уже бан. Осталось {remaining_days} дн.", reply_markup=markup)
            set_state(admin_id, 'pending_ban_target', target_id)
            set_state(admin_id, 'pending_ban_reason', reason)
            set_state(admin_id, 'pending_ban_duration', duration)
        clear_state(admin_id, 'awaiting_ban_target')
        return

    success, error = ban_user(target_id, duration_days=duration, reason=reason, banned_by=admin_id)
    if success:
        log_admin_action(admin_id, "BAN", target_id, f"{duration if duration else 'навсегда'}, {reason}")
        bot.send_message(message.chat.id, f"✅ Пользователь {target_id} забанен.")
        notify_group(f"<blockquote>🚨 Админ {admin_id} забанил {target_id}</blockquote>")
    else:
        bot.send_message(message.chat.id, f"❌ {error}")
    clear_state(admin_id, 'awaiting_ban_target')

def process_unban_target_input(message):
    admin_id = message.from_user.id
    try:
        target_id = int(message.text.strip())
    except:
        bot.send_message(message.chat.id, "❌ ID должен быть числом.")
        return
    is_violation, violator_id = check_untouchable(target_id, admin_id, "разбан")
    if is_violation:
        if violator_id == "owner_unban_attempt":
            bot.send_message(message.chat.id, "❌ Владелец не может быть забанен или разбанен.")
            clear_state(admin_id, 'awaiting_unban_target')
            return
        bot.send_message(message.chat.id, "❌ Попытка взаимодействия с неприкасаемым. Вы забанены навсегда!")
        clear_state(admin_id, 'awaiting_unban_target')
        return
    unban_user(target_id)
    log_admin_action(admin_id, "UNBAN", target_id)
    bot.send_message(message.chat.id, f"✅ Бан с {target_id} снят.")
    clear_state(admin_id, 'awaiting_unban_target')

def process_remove_nick_target_input(message):
    admin_id = message.from_user.id
    try:
        target_id = int(message.text.strip())
    except:
        bot.send_message(message.chat.id, "❌ ID должен быть числом.")
        return
    is_violation, violator_id = check_untouchable(target_id, admin_id, "удаление ника")
    if is_violation:
        bot.send_message(message.chat.id, "❌ Попытка удалить ник неприкасаемого. Вы забанены навсегда!")
        clear_state(admin_id, 'awaiting_remove_nick_target')
        return
    if remove_user_nickname(target_id):
        bot.send_message(message.chat.id, f"✅ Ник {target_id} удалён.")
        log_admin_action(admin_id, "REMOVE_NICK", target_id)
    else:
        bot.send_message(message.chat.id, "❌ Пользователь не найден.")
    clear_state(admin_id, 'awaiting_remove_nick_target')

def process_msg_user_target_input(message):
    admin_id = message.from_user.id
    text = message.text.strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        bot.send_message(message.chat.id, "❌ Формат: ID текст")
        return
    try:
        target_id = int(parts[0])
    except:
        bot.send_message(message.chat.id, "❌ ID должен быть числом.")
        return
    msg_text = parts[1]
    quick_target = get_state(admin_id, 'quick_msg_target')
    if quick_target:
        target_id = quick_target
        clear_state(admin_id, 'quick_msg_target')
    if target_id == UNTOUCHABLE_USER_ID and admin_id != UNTOUCHABLE_USER_ID:
        log_admin_action(admin_id, "MSG_UNTOUCHABLE", target_id, msg_text[:100])
        bot.send_message(message.chat.id, "⚠️ Сообщение неприкасаемому залогировано.")
    try:
        bot.send_message(target_id, f"📨 Сообщение от администратора:\n\n{msg_text}")
        bot.send_message(message.chat.id, f"✅ Отправлено пользователю {target_id}")
        log_admin_action(admin_id, "MSG_USER", target_id, msg_text[:100])
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
    clear_state(admin_id, 'awaiting_msg_user_target')

# ========== CALLBACK ОБРАБОТЧИКИ (ОСНОВНОЙ) ==========
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id = call.from_user.id
    banned, *_ = is_banned(user_id)
    if banned:
        bot.answer_callback_query(call.id, "Вы забанены")
        return

    data = call.data

    # Голосование
    if data.startswith('vote_'):
        parts = data.split('_')
        post_id = int(parts[1])
        vote_type = parts[2]
        post = get_post_by_id(post_id)
        if not post:
            bot.answer_callback_query(call.id, "Пост не найден")
            return
        post_user_id = post[1]
        if user_id == post_user_id:
            bot.answer_callback_query(call.id, "❌ Нельзя голосовать за свой пост")
            return
        existing_vote = get_vote(post_id, user_id)
        if existing_vote:
            bot.answer_callback_query(call.id, "❌ Вы уже проголосовали")
            return
        if vote_type == 'like':
            add_reputation(post_user_id, 1)
        else:
            add_reputation(post_user_id, -1)
        set_vote(post_id, user_id, vote_type)
        likes, dislikes = update_post_likes(post_id)
        new_markup = get_vote_keyboard(post_id)
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=new_markup)
        bot.answer_callback_query(call.id, f"Вы проголосовали: {'👍' if vote_type == 'like' else '👎'}")
        return

    # Модерация постов
    if data.startswith('approve_post_') or data.startswith('reject_post_'):
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        action, post_id = data.split('_')[:2]
        post_id = int(data.split('_')[-1])
        post = get_post_by_id(post_id)
        if not post:
            bot.answer_callback_query(call.id, "Пост не найден")
            return
        # Распаковка с учётом нового поля media_type
        if len(post) >= 9:
            pid, puid, pnick, pmedia_group, pfile_ids, pcaption, pstatus, pas_admin, media_type = post
        else:
            pid, puid, pnick, pmedia_group, pfile_ids, pcaption, pstatus, pas_admin = post
            media_type = 'text'  # fallback для старых записей
        if pstatus != 'pending':
            bot.answer_callback_query(call.id, f"Пост уже обработан ({pstatus})")
            return

        if action == 'approve':
            file_ids = pfile_ids.split(',') if pfile_ids else []
            reputation = get_user_reputation(puid)
            safe_nick = escape_html(pnick)

            # Заголовок
            if pas_admin:
                channel_header = (
                    f"👑 <b>АДМИНИСТРАЦИЯ</b>\n"
                    f"<code>{'─' * 20}</code>\n"
                    f"👤 {safe_nick}  │  🆔 <code>{puid}</code>\n"
                )
            else:
                channel_header = (
                    f"<b>📢 Анонимный шепотом вещает..</b>\n"
                    f"<code>{'─' * 20}</code>\n"
                    f"👤 Пользователь \"<i>{safe_nick}</i>\" пишет:\n"
                )
            channel_header += f"🥇 Репутация: {reputation}\n"
            channel_header += f"<code>{'─' * 20}</code>"

            safe_caption = escape_html(pcaption) if pcaption else ""
            MAX_CAPTION_LEN = 1024
            header_len = len(channel_header) + 4
            max_text_len = MAX_CAPTION_LEN - header_len
            if max_text_len < 10:
                max_text_len = 200
            if len(safe_caption) > max_text_len:
                safe_caption = safe_caption[:max_text_len - 3] + "..."
            if safe_caption:
                full_text = f"{channel_header}\n\n<blockquote>{safe_caption}</blockquote>"
            else:
                full_text = channel_header
            if len(full_text) > MAX_CAPTION_LEN:
                full_text = full_text[:MAX_CAPTION_LEN - 3] + "..."

            try:
                sent_messages = []
                if media_type == 'text' or not file_ids or (len(file_ids) == 1 and not file_ids[0]):
                    msg = bot.send_message(CHANNEL_ID, full_text, parse_mode='HTML',
                                           reply_markup=get_vote_keyboard(post_id))
                    sent_messages.append(msg.message_id)
                elif media_type == 'photo':
                    if len(file_ids) == 1:
                        msg = bot.send_photo(CHANNEL_ID, file_ids[0], caption=full_text,
                                             parse_mode='HTML', reply_markup=get_vote_keyboard(post_id))
                        sent_messages.append(msg.message_id)
                    else:
                        media_list = []
                        for i, fid in enumerate(file_ids):
                            if i == 0:
                                media_list.append(types.InputMediaPhoto(fid, caption=full_text, parse_mode='HTML'))
                            else:
                                media_list.append(types.InputMediaPhoto(fid))
                        msgs = bot.send_media_group(CHANNEL_ID, media_list)
                        sent_messages = [msg.message_id for msg in msgs]
                        if sent_messages:
                            bot.edit_message_reply_markup(CHANNEL_ID, sent_messages[0],
                                                          reply_markup=get_vote_keyboard(post_id))
                elif media_type == 'video':
                    if file_ids:
                        msg = bot.send_video(CHANNEL_ID, file_ids[0], caption=full_text,
                                             parse_mode='HTML', reply_markup=get_vote_keyboard(post_id))
                        sent_messages.append(msg.message_id)
                    else:
                        raise Exception("Видеофайл не найден")
                else:
                    raise Exception(f"Неизвестный тип медиа: {media_type}")

                if sent_messages:
                    update_post_status(post_id, 'approved', sent_messages)
                    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
                    bot.send_message(call.message.chat.id, f"✅ Пост #{post_id} одобрен и опубликован.")
                    try:
                        bot.send_message(puid, "✅ Ваш анонимный пост одобрен и опубликован в канале!")
                    except: pass
                else:
                    raise Exception("Не удалось отправить ни одного сообщения")

            except Exception as e:
                error_msg = f"❌ Ошибка публикации поста #{post_id}:\n{str(e)[:200]}"
                print(f"!!! ОШИБКА ПУБЛИКАЦИИ: {e}")
                bot.answer_callback_query(call.id, "Ошибка публикации")
                bot.send_message(call.message.chat.id, error_msg)
        else:
            update_post_status(post_id, 'rejected')
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
            bot.send_message(call.message.chat.id, f"❌ Пост #{post_id} отклонён.")
            try:
                bot.send_message(puid, "❌ Ваш анонимный пост отклонён администратором.")
            except: pass
        return

    # Админ-панель
    if data.startswith('admin_'):
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        if data == 'admin_back_to_main':
            bot.edit_message_text("🔧 Админ-панель:", call.message.chat.id, call.message.message_id, reply_markup=get_admin_main_inline_keyboard())
            return
        if data == 'admin_actions_tab':
            bot.edit_message_text("🎯 Действия:\n\nВыберите действие:", call.message.chat.id, call.message.message_id, reply_markup=get_admin_actions_keyboard())
        elif data == 'admin_users_tab':
            users = get_all_users()
            if not users:
                bot.edit_message_text("Нет пользователей.", call.message.chat.id, call.message.message_id)
                return
            set_state(user_id, 'admin_users_list', users)
            set_state(user_id, 'admin_users_page', 0)
            bot.edit_message_text("📋 Список:", call.message.chat.id, call.message.message_id, reply_markup=get_admin_users_keyboard(users, 0))
        elif data.startswith('admin_users_page_'):
            page = int(data.split('_')[-1])
            users = get_state(user_id, 'admin_users_list')
            if users:
                set_state(user_id, 'admin_users_page', page)
                bot.edit_message_text("📋 Список:", call.message.chat.id, call.message.message_id, reply_markup=get_admin_users_keyboard(users, page))
        elif data.startswith('admin_user_'):
            target_id = int(data.split('_')[-1])
            nick = get_user_nickname(target_id) or "не установлен"
            rep = get_user_reputation(target_id)
            text = f"👤 Информация о пользователе:\n\nID: {target_id}\nНик: {nick}\nРепутация: {rep}"
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=get_admin_user_actions_keyboard(target_id))
        elif data == 'admin_stats_tab':
            text, markup = get_admin_stats_keyboard()
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        elif data == 'admin_info_tab':
            info_text, markup = get_admin_info_keyboard()
            bot.edit_message_text(info_text, call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
        elif data == 'admin_ban_menu':
            bot.edit_message_text("Выберите срок:", call.message.chat.id, call.message.message_id, reply_markup=get_ban_duration_keyboard())
        elif data == 'admin_unban':
            set_state(user_id, 'awaiting_unban_target', True)
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️ Отмена", callback_data='admin_actions_tab'))
            bot.edit_message_text("Введите ID для разбана:", call.message.chat.id, call.message.message_id, reply_markup=markup)
        elif data == 'admin_remove_nick':
            set_state(user_id, 'awaiting_remove_nick_target', True)
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️ Отмена", callback_data='admin_actions_tab'))
            bot.edit_message_text("Введите ID для удаления ника:", call.message.chat.id, call.message.message_id, reply_markup=markup)
        elif data == 'admin_msg_user':
            set_state(user_id, 'awaiting_msg_user_target', True)
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️ Отмена", callback_data='admin_actions_tab'))
            bot.edit_message_text("Введите ID и текст:\n<code>ID текст</code>", call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)
        elif data == 'admin_banlist':
            bans = get_all_bans()
            if not bans:
                markup = types.InlineKeyboardMarkup()
                markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data='admin_actions_tab'))
                bot.edit_message_text("Нет банов.", call.message.chat.id, call.message.message_id, reply_markup=markup)
                return
            text = "🚫 Список банов:\n\n"
            for row in bans:
                uid, until, reason, by, _ = row
                until_str = "навсегда" if until == -1 else datetime.fromtimestamp(until).strftime('%Y-%m-%d %H:%M')
                text += f"ID: {uid}\nДо: {until_str}\nПричина: {reason}\nКем: {by}\n\n"
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data='admin_actions_tab'))
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
        elif data == 'admin_broadcast':
            set_state(user_id, 'awaiting_broadcast', True)
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("⬅️ Отмена", callback_data='admin_actions_tab'))
            bot.edit_message_text("Введите текст для рассылки всем пользователям:",
                                  call.message.chat.id, call.message.message_id, reply_markup=markup)
        return

    # Быстрые действия над пользователем из админки
    if data.startswith('admin_quickban_'):
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        parts = data.split('_')
        if len(parts) < 5:
            bot.answer_callback_query(call.id, "❌ Неверные данные")
            return
        target_id = int(parts[3])
        duration_str = parts[4]

        if duration_str == '1':
            days = 1
        elif duration_str == '7':
            days = 7
        elif duration_str == 'perm':
            days = None
        else:
            bot.answer_callback_query(call.id, "Неизвестный срок")
            return

        is_violation, _ = check_untouchable(target_id, user_id, "бан")
        if is_violation:
            bot.answer_callback_query(call.id, "❌ Нельзя забанить неприкасаемого!")
            return
        if target_id in ADMIN_IDS and target_id != user_id:
            handle_admin_vs_admin_ban(user_id, target_id, "Быстрый бан")
            bot.edit_message_text(
                f"❌ Вы попытались забанить администратора. Оба забанены.",
                call.message.chat.id, call.message.message_id
            )
            bot.answer_callback_query(call.id, "Администратор забанен")
            return

        success, error = ban_user(target_id, duration_days=days, reason="Быстрый бан", banned_by=user_id)
        if success:
            bot.answer_callback_query(call.id, f"✅ Пользователь {target_id} забанен")
            nick = get_user_nickname(target_id) or "не установлен"
            rep = get_user_reputation(target_id)
            text = f"👤 Информация о пользователе:\n\nID: {target_id}\nНик: {nick}\nРепутация: {rep}"
            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=get_admin_user_actions_keyboard(target_id)
            )
        else:
            bot.answer_callback_query(call.id, f"❌ {error}")

    elif data.startswith('admin_quickmsg_'):
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        parts = data.split('_')
        if len(parts) < 4:
            return
        target_id = int(parts[3])

        set_state(user_id, 'awaiting_msg_user_target', True)
        set_state(user_id, 'quick_msg_target', target_id)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⬅️ Отмена", callback_data=f'admin_user_{target_id}'))
        bot.edit_message_text(
            f"✏️ Введите сообщение для пользователя {target_id}:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        bot.answer_callback_query(call.id)

    elif data.startswith('admin_quickremove_'):
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        parts = data.split('_')
        if len(parts) < 4:
            return
        target_id = int(parts[3])

        is_violation, _ = check_untouchable(target_id, user_id, "удаление ника")
        if is_violation:
            bot.answer_callback_query(call.id, "❌ Нельзя удалить ник неприкасаемого!")
            return

        if remove_user_nickname(target_id):
            bot.answer_callback_query(call.id, f"✅ Ник пользователя {target_id} удалён")
            nick = "не установлен"
            rep = get_user_reputation(target_id)
            text = f"👤 Информация о пользователе:\n\nID: {target_id}\nНик: {nick}\nРепутация: {rep}"
            bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=get_admin_user_actions_keyboard(target_id)
            )
        else:
            bot.answer_callback_query(call.id, "❌ Не удалось удалить ник")

    elif data.startswith('admin_quickunban_'):
        if user_id not in ADMIN_IDS:
            bot.answer_callback_query(call.id, "Нет прав")
            return
        parts = data.split('_')
        if len(parts) < 4:
            return
        target_id = int(parts[3])

        if target_id == UNTOUCHABLE_USER_ID and user_id != UNTOUCHABLE_USER_ID:
            bot.answer_callback_query(call.id, "❌ Владелец не может быть разбанен")
            return

        unban_user(target_id)
        bot.answer_callback_query(call.id, f"✅ Бан с {target_id} снят")
        nick = get_user_nickname(target_id) or "не установлен"
        rep = get_user_reputation(target_id)
        text = f"👤 Информация о пользователе:\n\nID: {target_id}\nНик: {nick}\nРепутация: {rep}"
        bot.edit_message_text(
            text,
            call.message.chat.id,
            call.message.message_id,
            reply_markup=get_admin_user_actions_keyboard(target_id)
        )

    elif data.startswith('ban_dur_'):
        if user_id not in ADMIN_IDS: return
        if data == 'ban_dur_1':
            set_state(user_id, 'ban_duration', 1); set_state(user_id, 'ban_perm', False)
        elif data == 'ban_dur_7':
            set_state(user_id, 'ban_duration', 7); set_state(user_id, 'ban_perm', False)
        elif data == 'ban_dur_perm':
            set_state(user_id, 'ban_duration', None); set_state(user_id, 'ban_perm', True)
        set_state(user_id, 'awaiting_ban_target', True)
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("⬅️ Отмена", callback_data='admin_actions_tab'))
        bot.edit_message_text("Введите ID и причину:\n<code>ID причина</code>", call.message.chat.id, call.message.message_id, parse_mode='HTML', reply_markup=markup)

    elif data.startswith('add_ban_time_'):
        if user_id not in ADMIN_IDS: return
        target_id = int(data.split('_')[-1])
        reason = get_state(user_id, 'pending_ban_reason') or 'Не указана'
        duration = get_state(user_id, 'pending_ban_duration')
        success, error = ban_user(target_id, duration_days=duration, reason=reason, banned_by=user_id, add_time=True)
        if success:
            bot.edit_message_text(f"✅ Время бана {target_id} увеличено.", call.message.chat.id, call.message.message_id)
        else:
            bot.edit_message_text(f"❌ {error}", call.message.chat.id, call.message.message_id)
        for k in ['pending_ban_target', 'pending_ban_reason', 'pending_ban_duration', 'pending_ban_perm']:
            clear_state(user_id, k)

    elif data.startswith('force_unban_'):
        if user_id not in ADMIN_IDS: return
        target_id = int(data.split('_')[-1])
        unban_user(target_id)
        bot.edit_message_text(f"✅ Бан с {target_id} снят.", call.message.chat.id, call.message.message_id)
        notify_group(f"<blockquote>🚨 Админ {user_id} разбанил {target_id}</blockquote>")

    elif data.startswith('approve_delete_') or data.startswith('reject_delete_'):
        if user_id not in ADMIN_IDS: return
        action, request_id = data.split('_')[:2]
        request_id = int(data.split('_')[-1])
        req = get_request_by_id(request_id)
        if not req:
            bot.edit_message_text("❌ Заявка не найдена или истекла.", call.message.chat.id, call.message.message_id)
            return
        req_id, target_id, reason, status, _ = req
        if status != 'pending':
            bot.edit_message_text(f"❌ Уже обработана ({status}).", call.message.chat.id, call.message.message_id)
            return
        if action == 'approve':
            remove_user_nickname(target_id)
            update_request_status(request_id, 'approved')
            bot.edit_message_text(f"✅ Ник {target_id} удалён.", call.message.chat.id, call.message.message_id)
            try:
                bot.send_message(target_id, "✅ Заявка на удаление ника одобрена!")
            except: pass
        else:
            update_request_status(request_id, 'rejected', update_reject_time=True)
            bot.edit_message_text(f"❌ Заявка отклонена.", call.message.chat.id, call.message.message_id)
            try:
                bot.send_message(target_id, "❌ Заявка на удаление ника отклонена. Повторно через 24 часа.")
            except: pass

    elif data.startswith('owner_'):
        if user_id != UNTOUCHABLE_USER_ID:
            bot.answer_callback_query(call.id, "❌ Только владелец")
            return
        parts = data.split('_')
        if len(parts) < 4: return
        action = parts[1]
        if action in ['unban', 'keepban'] and 'admin1' in data:
            sub_action = parts[1]
            admin1_id = int(parts[3]); admin2_id = int(parts[4]); timestamp = int(parts[5])
            if sub_action == 'unban':
                unban_user(admin1_id)
                if admin1_id not in ADMIN_IDS: ADMIN_IDS.append(admin1_id)
                bot.edit_message_text(f"✅ Администратор {admin1_id} разбанен.", call.message.chat.id, call.message.message_id)
                try: bot.send_message(admin1_id, "✅ Вы разбанены владельцем.")
                except: pass
            else:
                bot.edit_message_text(f"🔨 Администратор {admin1_id} оставлен в бане.", call.message.chat.id, call.message.message_id)
        elif action in ['unban', 'keepban'] and 'admin2' in data:
            sub_action = parts[1]
            admin1_id = int(parts[3]); admin2_id = int(parts[4]); timestamp = int(parts[5])
            if sub_action == 'unban':
                unban_user(admin2_id)
                if admin2_id not in ADMIN_IDS: ADMIN_IDS.append(admin2_id)
                bot.edit_message_text(f"✅ Администратор {admin2_id} разбанен.", call.message.chat.id, call.message.message_id)
                try: bot.send_message(admin2_id, "✅ Вы разбанены владельцем.")
                except: pass
            else:
                bot.edit_message_text(f"🔨 Администратор {admin2_id} оставлен в бане.", call.message.chat.id, call.message.message_id)
        elif action == 'unban' and 'both' in data:
            admin1_id = int(parts[3]); admin2_id = int(parts[4]); timestamp = int(parts[5])
            unban_user(admin1_id); unban_user(admin2_id)
            if admin1_id not in ADMIN_IDS: ADMIN_IDS.append(admin1_id)
            if admin2_id not in ADMIN_IDS: ADMIN_IDS.append(admin2_id)
            bot.edit_message_text(f"✅ Оба администратора разбанены.", call.message.chat.id, call.message.message_id)
            notify_group(f"<blockquote>✅ Владелец разбанил {admin1_id} и {admin2_id}</blockquote>")
            try:
                bot.send_message(admin1_id, "✅ Вы разбанены владельцем.")
                bot.send_message(admin2_id, "✅ Вы разбанены владельцем.")
            except: pass
        elif action in ['unban', 'keepban'] and len(parts) == 5:
            admin_id = int(parts[2]); action_type = parts[3]; timestamp = int(parts[4])
            if action == 'unban':
                unban_user(admin_id)
                if admin_id not in ADMIN_IDS: ADMIN_IDS.append(admin_id)
                bot.edit_message_text(f"✅ Администратор {admin_id} разбанен.", call.message.chat.id, call.message.message_id)
                notify_group(f"<blockquote>✅ Владелец разбанил {admin_id}</blockquote>")
                try: bot.send_message(admin_id, "✅ Вы разбанены владельцем.")
                except: pass
            else:
                bot.edit_message_text(f"🔨 Администратор {admin_id} оставлен в бане.", call.message.chat.id, call.message.message_id)
                bot.answer_callback_query(call.id, "Админ оставлен в бане")

# ========== ФОНОВАЯ ЗАДАЧА ==========
def expire_requests_loop():
    while True:
        time.sleep(3600)
        expired = expire_old_requests()
        if expired:
            print(f"Помечено {expired} просроченных заявок")

# ========== ЗАПУСК ==========
if __name__ == '__main__':
    threading.Thread(target=expire_requests_loop, daemon=True).start()
    print("Бот запущен...")
    bot.polling(none_stop=True)