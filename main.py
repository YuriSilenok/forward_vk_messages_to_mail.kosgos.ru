import os
import re
import json
import time
import logging
import mimetypes
import smtplib
import requests
import random
import vk_api
import threading
import html

from datetime import datetime
from pathlib import Path
from urllib.parse import unquote
from email.message import EmailMessage
from email.utils import formataddr
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType
from vk_api.keyboard import VkKeyboard, VkKeyboardColor

# ==================== НАСТРОЙКИ ====================
VK_GROUP_TOKEN = 'vk1.a.bkc0VU4cqUYUcXbPGTxN2DCvjLsWf4LQcoleFcNkY48cwQeCJAtBVXuGjze2461b6YZL1mb9iuiPis1JggxV2gJESBR6wLr50eR9ox4tfOHP78anWuVGUegelwPJIuG-oNiH11c0Mc7lxBECwOM8VcJjqiWtsz24xrXtC_HJCgeKNJed3HaZr9Hg-Km6DIzi0r70DMnpflpGzC9jHQkAXA'
VK_GROUP_ID = 242696420

# SMTP НАСТРОЙКИ (настраивает разработчик)
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_USER = 'dadlyvernon123@gmail.com'
SMTP_PASSWORD = 'seei ubde ijyl xkke'
MAIL_FROM_NAME = "VK Bot"

# Главные администраторы (только они могут добавлять других админов)
ROOT_ADMIN_IDS = {287029514}

CONFIG_FILE = Path("config.json")
MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_TOTAL_ATTACHMENTS = 20 * 1024 * 1024

DIGEST_MAX_MESSAGES = 15
DIGEST_TIMEOUT_SEC = 15 * 60

user_states = {}
user_cache = {}
chat_cache = {}
message_buffer = {}
buffer_lock = threading.Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ==================== КОНФИГУРАЦИЯ ====================
def load_config():
    if not CONFIG_FILE.exists():
        return {"chats": {}, "admins": []}
    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)

    # Защита от старого формата файла
    if "chats" not in data:
        return {"chats": data, "admins": []}
    return data


def save_config(config):
    temp_file = CONFIG_FILE.with_suffix(".tmp")
    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=4)
    os.replace(temp_file, CONFIG_FILE)


def is_manager(user_id, config):
    return user_id in ROOT_ADMIN_IDS or user_id in config.get("admins", [])


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_user_name(vk, user_id):
    if user_id in user_cache: return user_cache[user_id]
    try:
        user = vk.users.get(user_ids=user_id)[0]
        name = (user.get("first_name", "") + " " + user.get("last_name", "")).strip()
    except Exception:
        name = f"ID {user_id}"
    user_cache[user_id] = name
    return name


def get_chat_title(vk, peer_id):
    if peer_id < 2000000000: return "Личный диалог"
    if peer_id in chat_cache: return chat_cache[peer_id]
    try:
        response = vk.messages.getConversationsById(peer_ids=peer_id)
        items = response.get("items", [])
        if items:
            title = items[0].get("chat_settings", {}).get("title")
            if title:
                chat_cache[peer_id] = title
                return title
    except Exception:
        pass
    return f"Беседа {peer_id}"


def format_timestamp(timestamp):
    if not timestamp: return "Время неизвестно"
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return str(timestamp)


# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard(is_root=False):
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button("Добавить почту", color=VkKeyboardColor.POSITIVE)
    keyboard.add_button("Удалить почту", color=VkKeyboardColor.NEGATIVE)
    keyboard.add_line()
    keyboard.add_button("Список почт", color=VkKeyboardColor.PRIMARY)

    # Если это главный админ, добавляем кнопку "Админы" в этот же ряд или отдельный
    if is_root:
        keyboard.add_button("Админы", color=VkKeyboardColor.SECONDARY)

    # Кнопка помощи всегда в самом низу
    keyboard.add_line()
    keyboard.add_button("Помощь", color=VkKeyboardColor.SECONDARY)

    return keyboard.get_keyboard()


def get_cancel_keyboard():
    keyboard = VkKeyboard(one_time=True)
    keyboard.add_button("Отмена", color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()


def get_admins_keyboard():
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button("Добавить админа", color=VkKeyboardColor.POSITIVE)
    keyboard.add_button("Удалить админа", color=VkKeyboardColor.NEGATIVE)
    keyboard.add_line()
    keyboard.add_button("Назад", color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()


# ==================== ОТПРАВКА СООБЩЕНИЙ VK ====================
def send_vk_message(vk, peer_id, message, keyboard=None):
    try:
        params = {"peer_id": peer_id, "message": message, "random_id": random.randint(1, 2 ** 31 - 1)}
        if keyboard: params["keyboard"] = keyboard
        vk.messages.send(**params)
    except Exception as error:
        logging.error(f"Ошибка VK: {error}")


def send_long_vk_message(vk, peer_id, message, keyboard=None):
    limit = 3500
    while message:
        part = message[:limit]
        if len(message) > limit:
            split_position = part.rfind("\n")
            if split_position > 0: part = part[:split_position]
        send_vk_message(vk, peer_id, part, keyboard)
        message = message[len(part):]


# ==================== СКАЧИВАНИЕ И ПОЧТА ====================
def download_file(url):
    if not url.startswith(("https://", "http://")): raise ValueError("Некорректный URL")
    with requests.get(url, stream=True, timeout=(10, 60)) as response:
        response.raise_for_status()
        content_length = int(response.headers.get("Content-Length", 0))
        if content_length > MAX_FILE_SIZE: raise ValueError("Файл > 20 МБ")
        chunks = []
        total_size = 0
        for chunk in response.iter_content(8192):
            if not chunk: continue
            total_size += len(chunk)
            if total_size > MAX_FILE_SIZE: raise ValueError("Файл > 20 МБ")
            chunks.append(chunk)
        filename = "attachment"
        disposition = response.headers.get("Content-Disposition", "")
        match = re.search(r"filename\*?=(?:UTF-8'')?([^;]+)", disposition, re.IGNORECASE)
        if match:
            filename = unquote(match.group(1).strip().strip('"'))
        else:
            filename = unquote(url.split("?")[0].rstrip("/").split("/")[-1])
        return Path(filename).name or "attachment", b"".join(chunks)


def get_attachment_urls(msg):
    result = []
    for attachment in msg.get("attachments", []):
        att_type = attachment.get("type")
        try:
            if att_type == "photo":
                sizes = attachment["photo"].get("sizes", [])
                if sizes:
                    best = max(sizes, key=lambda x: (x.get("width", 0) * x.get("height", 0)))
                    if best.get("url"): result.append((f"photo_{attachment['photo']['id']}.jpg", best["url"]))
            elif att_type == "doc":
                if attachment["doc"].get("url"): result.append(
                    (attachment["doc"].get("title", "document"), attachment["doc"]["url"]))
            elif att_type == "video":
                v = attachment["video"]
                if v.get("owner_id") and v.get("id"): result.append(
                    ("__LINK__", f"https://vk.com/video{v['owner_id']}_{v['id']}", v.get("title", "Видео")))
        except Exception:
            pass
    return result


def process_attachments(msg):
    attachments, links, total_size = [], [], 0
    for item in get_attachment_urls(msg):
        if item[0] == "__LINK__":
            links.append(f"{item[2]}: {item[1]}")
            continue
        filename, url = item
        try:
            downloaded_name, data = download_file(url)
            if total_size + len(data) > MAX_TOTAL_ATTACHMENTS:
                links.append(f"{filename}: превышен лимит")
                continue
            total_size += len(data)
            attachments.append((downloaded_name, data))
        except Exception:
            links.append(f"{filename}: не удалось скачать")
    return attachments, links


def send_email_with_attachments(subject, body_text, body_html, attachments, recipients):
    if not recipients: return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((MAIL_FROM_NAME, SMTP_USER))
    message["Bcc"] = ", ".join(recipients)
    message.set_content(body_text, charset="utf-8")
    if body_html: message.add_alternative(body_html, subtype="html")

    for filename, data in attachments:
        c_type, c_enc = mimetypes.guess_type(filename)
        if not c_type or c_enc: c_type = "application/octet-stream"
        maintype, subtype = c_type.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    try:
        if SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT, timeout=30)
        else:
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30)
            server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(message)
        server.quit()
        return True
    except Exception as e:
        logging.exception("Ошибка SMTP")
        return False


# ==================== ДАЙДЖЕСТ ====================
def flush_digest(vk, peer_id, config):
    with buffer_lock:
        if peer_id not in message_buffer or not message_buffer[peer_id]['messages']: return
        digest_data = message_buffer[peer_id]
        del message_buffer[peer_id]

    recipients = config.get("chats", {}).get(str(peer_id), [])
    if not recipients: return

    chat_title = get_chat_title(vk, peer_id)
    msg_count = len(digest_data['messages'])
    start_time = format_timestamp(digest_data['first_time'])

    plain_lines = [f"Дайджест из чата: {chat_title}", f"Сообщений: {msg_count}", "=" * 40, ""]
    html_content = f"""
    <div style="font-family: sans-serif; background-color: #f4f5f7; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden;">
            <div style="background-color: #2787F5; color: white; padding: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 20px;">Дайджест: {chat_title}</h2>
                <p style="margin: 5px 0 0 0; font-size: 14px;">Сообщений: {msg_count} | Начало: {start_time}</p>
            </div>
            <div style="padding: 20px;">
    """
    all_attachments, total_attach_size = [], 0

    for i, msg_info in enumerate(digest_data['messages'], 1):
        user, time_str, text = msg_info['user'], msg_info['time'], msg_info['text']
        safe_text = html.escape(text).replace('\n', '<br>') if text else '<i style="color: #939393;">(без текста)</i>'
        plain_lines.append(f"[{i}] {user} ({time_str}):\n{text if text else '(без текста)'}\n")

        html_content += f"""
                <div style="margin-bottom: 20px; padding-bottom: 15px; border-bottom: 1px solid #e5e5e5;">
                    <div style="margin-bottom: 8px;">
                        <span style="font-weight: bold; color: #000; font-size: 15px;">{user}</span>
                        <span style="color: #939393; font-size: 12px; margin-left: 8px;">{time_str}</span>
                    </div>
                    <div style="color: #333; font-size: 14px; line-height: 1.5; background-color: #f0f2f5; padding: 10px 14px; border-radius: 10px;">
                        {safe_text}
                    </div>
        """
        for link in msg_info['links']:
            html_content += f"<div style='margin-top: 8px;'><a href='{link}' style='color: #2787F5;'>🔗 {link}</a></div>"
            plain_lines.append(f"Ссылка: {link}")

        for fname, fdata in msg_info['attachments']:
            if total_attach_size + len(fdata) <= MAX_TOTAL_ATTACHMENTS:
                all_attachments.append((f"msg{i}_{fname}", fdata))
                total_attach_size += len(fdata)
                html_content += f"<div style='margin-top: 8px; font-size: 13px; color: #555;'>📎 <b>Вложение:</b> {fname}</div>"
                plain_lines.append(f"Вложение: {fname}")
        html_content += "</div>"

    html_content += "</div></div></div>"
    send_email_with_attachments(f"Дайджест: {chat_title} ({msg_count})", "\n".join(plain_lines), html_content,
                                all_attachments, recipients)


def digest_worker(vk, config):
    while True:
        time.sleep(10)
        now = time.time()
        peers_to_flush = []
        with buffer_lock:
            for peer_id, data in message_buffer.items():
                if now - data['first_time'] >= DIGEST_TIMEOUT_SEC: peers_to_flush.append(peer_id)
        for peer_id in peers_to_flush:
            flush_digest(vk, peer_id, config)


def handle_chat_message(vk, msg, config):
    peer_id, from_id = msg.get("peer_id"), msg.get("from_id")
    if not config.get("chats", {}).get(str(peer_id)):
        send_vk_message(vk, peer_id, f"⚠ Эта беседа ({peer_id}) не подключена к почте.")
        return

    attachments, links = process_attachments(msg)
    msg_data = {
        'user': get_user_name(vk, from_id),
        'time': format_timestamp(msg.get("date")),
        'text': msg.get("text", "").strip(),
        'attachments': attachments,
        'links': links
    }

    with buffer_lock:
        if peer_id not in message_buffer: message_buffer[peer_id] = {'messages': [], 'first_time': time.time()}
        message_buffer[peer_id]['messages'].append(msg_data)
        current_count = len(message_buffer[peer_id]['messages'])

    if current_count >= DIGEST_MAX_MESSAGES: flush_digest(vk, peer_id, config)


# ==================== АДМИН-ПАНЕЛЬ ====================
def process_admin_message(vk, user_id, peer_id, text, config):
    text = text.strip()
    is_root = user_id in ROOT_ADMIN_IDS

    if text.lower() in ("отмена", "/cancel"):
        user_states.pop(user_id, None)
        send_vk_message(vk, peer_id, "Отменено.", get_main_keyboard(is_root))
        return

    if text in ("Начать", "/start", "/menu", "Меню", "Назад"):
        user_states.pop(user_id, None)
        send_vk_message(vk, peer_id, "Главное меню:", get_main_keyboard(is_root))
        return

    if user_id in user_states:
        state_data = user_states[user_id]
        state = state_data["state"]

        if state == "waiting_admin_id_add":
            if not text.isdigit():
                send_vk_message(vk, peer_id, "ID должен быть числом:", get_cancel_keyboard())
                return
            new_id = int(text)
            if new_id in config.get("admins", []) or new_id in ROOT_ADMIN_IDS:
                response = "Этот пользователь уже админ."
            else:
                config.setdefault("admins", []).append(new_id)
                save_config(config)
                response = f"✅ {new_id} теперь админ."
            user_states.pop(user_id, None)
            send_vk_message(vk, peer_id, response, get_admins_keyboard())
            return

        if state == "waiting_admin_id_del":
            if not text.isdigit():
                send_vk_message(vk, peer_id, "ID должен быть числом:", get_cancel_keyboard())
                return
            del_id = int(text)
            if del_id in config.get("admins", []):
                config["admins"].remove(del_id)
                save_config(config)
                response = f"🗑 {del_id} лишён прав."
            else:
                response = "Такого админа нет."
            user_states.pop(user_id, None)
            send_vk_message(vk, peer_id, response, get_admins_keyboard())
            return

        if state in ("waiting_chat_id_add", "waiting_chat_id_del"):
            if not text.isdigit() or int(text) < 2000000000:
                send_vk_message(vk, peer_id, "Неверный ID беседы. Введи ещё раз:", get_cancel_keyboard())
                return
            action = "add" if state == "waiting_chat_id_add" else "del"
            user_states[user_id] = {"state": f"waiting_email_{action}", "chat_id": text}
            send_vk_message(vk, peer_id, "Введи email:", get_cancel_keyboard())
            return

        if state == "waiting_email_add":
            chat_id, email = state_data["chat_id"], text.lower()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                send_vk_message(vk, peer_id, "Неверный email. Попробуй ещё раз:", get_cancel_keyboard())
                return
            config.setdefault("chats", {}).setdefault(chat_id, [])
            if email in config["chats"][chat_id]:
                response = "Этот email уже есть."
            else:
                config["chats"][chat_id].append(email)
                save_config(config)
                response = f"✅ {email} добавлен."
            user_states.pop(user_id, None)
            send_vk_message(vk, peer_id, response, get_main_keyboard(is_root))
            return

        if state == "waiting_email_del":
            chat_id, email = state_data["chat_id"], text.lower()
            if chat_id in config["chats"] and email in config["chats"][chat_id]:
                config["chats"][chat_id].remove(email)
                if not config["chats"][chat_id]: del config["chats"][chat_id]
                save_config(config)
                response = "🗑 Email удалён."
            else:
                response = "Такой email не найден."
            user_states.pop(user_id, None)
            send_vk_message(vk, peer_id, response, get_main_keyboard(is_root))
            return

    if text in ("Добавить почту", "/add"):
        user_states[user_id] = {"state": "waiting_chat_id_add"}
        send_vk_message(vk, peer_id, "Введи ID беседы:", get_cancel_keyboard())
    elif text in ("Удалить почту", "/del"):
        user_states[user_id] = {"state": "waiting_chat_id_del"}
        send_vk_message(vk, peer_id, "Введи ID беседы:", get_cancel_keyboard())

    elif text in ("Список почт", "/list"):
        chats = config.get("chats", {})
        if not chats:
            send_vk_message(vk, peer_id, "Список пуст.", get_main_keyboard(is_root))
        else:
            lines = ["Подключённые беседы:\n"]
            for chat_id, emails in chats.items():
                lines.append(f"{get_chat_title(vk, int(chat_id))} ({chat_id})")
                for email in emails: lines.append(f"  • {email}")
                lines.append("")
            send_long_vk_message(vk, peer_id, "\n".join(lines), get_main_keyboard(is_root))

    elif text in ("Помощь", "/help"):
        help_text = (
            "╭──────────────────────╮\n"
            "   🤖  О БОТЕ\n"
            "╰──────────────────────╯\n\n"
            "Этот бот пересылает сообщения из бесед ВК на вашу почту.\n\n"
            "⚙️ КАК ЭТО РАБОТАЕТ:\n"
            f"├ 📩 Бот собирает сообщения в «Дайджест».\n"
            f"├ ⏱ Письмо уходит раз в {DIGEST_TIMEOUT_SEC // 60} мин. или при {DIGEST_MAX_MESSAGES} сообщениях.\n"
            "├ 📎 Пересылаются фото, файлы и ссылки на видео.\n"
            "├ 🔒 Все получатели скрыты (Bcc).\n"
            "└ ✨ Письма приходят в красивом HTML-формате.\n\n"
            "🛠 УПРАВЛЕНИЕ:\n"
            "├ ➕ «Добавить почту» — привязать email к беседе.\n"
            "├ ➖ «Удалить почту» — отвязать email.\n"
            "└ 📋 «Список почт» — показать текущие привязки."
        )
        send_vk_message(vk, peer_id, help_text, get_main_keyboard(is_root))

    elif text == "Админы" and is_root:
        admins = config.get("admins", [])
        list_str = "\n".join([f"• {a}" for a in admins]) if admins else "Нет доп. админов."
        send_vk_message(vk, peer_id, f"Текущие админы:\n{list_str}", get_admins_keyboard())

    elif text == "Добавить админа" and is_root:
        user_states[user_id] = {"state": "waiting_admin_id_add"}
        send_vk_message(vk, peer_id, "Введи VK ID нового админа:", get_cancel_keyboard())

    elif text == "Удалить админа" and is_root:
        user_states[user_id] = {"state": "waiting_admin_id_del"}
        send_vk_message(vk, peer_id, "Введи VK ID админа для удаления:", get_cancel_keyboard())

    else:
        send_vk_message(vk, peer_id, "Используйте кнопки меню.", get_main_keyboard(is_root))


# ==================== ЗАПУСК ====================
def main():
    config = load_config()
    vk_session = vk_api.VkApi(token=VK_GROUP_TOKEN)
    vk = vk_session.get_api()

    threading.Thread(target=digest_worker, args=(vk, config), daemon=True).start()
    logging.info("Подключение к VK Long Poll...")

    while True:
        try:
            longpoll = VkBotLongPoll(vk_session, VK_GROUP_ID)
            logging.info("Бот запущен.")

            for event in longpoll.listen():
                if event.type != VkBotEventType.MESSAGE_NEW: continue
                msg = event.object.message
                if msg.get("out") == 1: continue

                peer_id, from_id = msg.get("peer_id"), msg.get("from_id")
                text = msg.get("text", "").strip()

                if peer_id == from_id:
                    if not is_manager(from_id, config):
                        send_vk_message(vk, peer_id, f"Нет прав администратора.\nВаш ID: {from_id}")
                        continue
                    process_admin_message(vk, from_id, peer_id, text, config)
                elif peer_id >= 2000000000:
                    handle_chat_message(vk, msg, config)

        except KeyboardInterrupt:
            break
        except Exception:
            logging.exception("Ошибка Long Poll. Переподключение...")
            time.sleep(5)


if __name__ == "__main__":
    main()