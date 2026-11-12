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
import html  # Добавлено для безопасной HTML-верстки

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
ADMIN_IDS = {287029514}

SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SMTP_USER = 'dadlyvernon123@gmail.com'
SMTP_PASSWORD = 'seei ubde ijyl xkke'
MAIL_FROM_NAME = "VK Bot"

CONFIG_FILE = Path("config.json")
MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_TOTAL_ATTACHMENTS = 20 * 1024 * 1024

# --- НАСТРОЙКИ ДАЙДЖЕСТА ---
DIGEST_MAX_MESSAGES = 15  # Отправить, если накопилось 15 сообщений
DIGEST_TIMEOUT_SEC = 15 * 60  # Отправить, если прошло 15 минут с первого сообщения

user_states = {}
user_cache = {}
chat_cache = {}

# Буфер для накопления сообщений: {peer_id: {'messages': [], 'first_time': timestamp}}
message_buffer = {}
buffer_lock = threading.Lock()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ==================== КОНФИГУРАЦИЯ ====================
def load_config():
    if not CONFIG_FILE.exists():
        return {}
    with CONFIG_FILE.open("r", encoding="utf-8") as file:
        data = json.load(file)
    result = {}
    for chat_id, emails in data.items():
        if str(chat_id).isdigit() and int(chat_id) >= 2000000000 and isinstance(emails, list):
            result[str(chat_id)] = [email.lower().strip() for email in emails if isinstance(email, str)]
    return result


def save_config(config):
    temp_file = CONFIG_FILE.with_suffix(".tmp")
    with temp_file.open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=4)
    os.replace(temp_file, CONFIG_FILE)


def is_manager(user_id):
    return user_id in ADMIN_IDS


# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_user_name(vk, user_id):
    if user_id in user_cache:
        return user_cache[user_id]
    try:
        user = vk.users.get(user_ids=user_id)[0]
        name = (user.get("first_name", "") + " " + user.get("last_name", "")).strip()
    except Exception:
        name = f"ID {user_id}"
    user_cache[user_id] = name
    return name


def get_chat_title(vk, peer_id):
    if peer_id < 2000000000:
        return "Личный диалог"
    if peer_id in chat_cache:
        return chat_cache[peer_id]
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
    if not timestamp:
        return "Время неизвестно"
    try:
        return datetime.fromtimestamp(int(timestamp)).strftime("%d.%m.%Y %H:%M:%S")
    except Exception:
        return str(timestamp)


# ==================== КЛАВИАТУРЫ ====================
def get_main_keyboard():
    keyboard = VkKeyboard(one_time=False)
    keyboard.add_button("Добавить почту", color=VkKeyboardColor.POSITIVE)
    keyboard.add_button("Удалить почту", color=VkKeyboardColor.NEGATIVE)
    keyboard.add_line()
    keyboard.add_button("Список почт", color=VkKeyboardColor.PRIMARY)
    keyboard.add_button("Статус бота", color=VkKeyboardColor.SECONDARY)
    keyboard.add_line()
    keyboard.add_button("Помощь", color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()


def get_cancel_keyboard():
    keyboard = VkKeyboard(one_time=True)
    keyboard.add_button("Отмена", color=VkKeyboardColor.SECONDARY)
    return keyboard.get_keyboard()


# ==================== ОТПРАВКА СООБЩЕНИЙ VK ====================
def send_vk_message(vk, peer_id, message, keyboard=None):
    try:
        params = {
            "peer_id": peer_id,
            "message": message,
            "random_id": random.randint(1, 2 ** 31 - 1)
        }
        if keyboard:
            params["keyboard"] = keyboard
        vk.messages.send(**params)
    except Exception as error:
        logging.error(f"Ошибка отправки сообщения VK: {error}")


def send_long_vk_message(vk, peer_id, message):
    limit = 3500
    while message:
        part = message[:limit]
        if len(message) > limit:
            split_position = part.rfind("\n")
            if split_position > 0:
                part = part[:split_position]
        send_vk_message(vk, peer_id, part, get_main_keyboard())
        message = message[len(part):]


# ==================== СКАЧИВАНИЕ ВЛОЖЕНИЙ ====================
def download_file(url):
    if not url or not url.startswith(("https://", "http://")):
        raise ValueError("Некорректный URL")
    with requests.get(url, stream=True, timeout=(10, 60)) as response:
        response.raise_for_status()
        content_length = int(response.headers.get("Content-Length", 0))
        if content_length > MAX_FILE_SIZE:
            raise ValueError("Файл > 20 МБ")
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
        filename = Path(filename).name or "attachment"
        return filename, b"".join(chunks)


def get_attachment_urls(msg):
    result = []
    for attachment in msg.get("attachments", []):
        att_type = attachment.get("type")
        try:
            if att_type == "photo":
                photo = attachment["photo"]
                sizes = photo.get("sizes", [])
                if sizes:
                    best = max(sizes, key=lambda item: (item.get("width", 0) * item.get("height", 0)))
                    if best.get("url"): result.append((f"photo_{photo['id']}.jpg", best["url"]))
            elif att_type == "doc":
                doc = attachment["doc"]
                if doc.get("url"): result.append((doc.get("title", "document"), doc["url"]))
            elif att_type == "video":
                video = attachment["video"]
                owner_id = video.get("owner_id")
                video_id = video.get("id")
                title = video.get("title", "Видео")
                if owner_id and video_id:
                    result.append(("__LINK__", f"https://vk.com/video{owner_id}_{video_id}", title))
        except Exception as error:
            logging.warning(f"Ошибка вложения: {error}")
    return result


def process_attachments(msg):
    attachments = []
    links = []
    total_size = 0
    for item in get_attachment_urls(msg):
        if item[0] == "__LINK__":
            _, url, title = item
            links.append(f"{title}: {url}")
            continue
        filename, url = item
        try:
            downloaded_name, data = download_file(url)
            if total_size + len(data) > MAX_TOTAL_ATTACHMENTS:
                links.append(f"{filename}: превышен лимит")
                continue
            total_size += len(data)
            if not downloaded_name or downloaded_name == "attachment":
                downloaded_name = filename
            attachments.append((downloaded_name, data))
        except Exception as error:
            logging.error(f"Ошибка скачивания {filename}: {error}")
            links.append(f"{filename}: не удалось скачать")
    return attachments, links


# ==================== ОТПРАВКА EMAIL (HTML) ====================
def send_email_with_attachments(subject, body_text, body_html, attachments, recipients):
    if not recipients: return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = formataddr((MAIL_FROM_NAME, SMTP_USER))
    message["Bcc"] = ", ".join(recipients)

    # Устанавливаем обычный текст (если почтовый клиент не поддерживает HTML)
    message.set_content(body_text, charset="utf-8")

    # Добавляем красивую HTML-версию
    if body_html:
        message.add_alternative(body_html, subtype="html")

    for filename, data in attachments:
        content_type, encoding = mimetypes.guess_type(filename)
        if not content_type or encoding: content_type = "application/octet-stream"
        maintype, subtype = content_type.split("/", 1)
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(message)
        logging.info(f"Письмо отправлено: {subject}")
        return True
    except Exception:
        logging.exception("Ошибка отправки Gmail")
        return False


# ==================== БУФЕРИЗАЦИЯ И ДАЙДЖЕСТ ====================
def flush_digest(vk, peer_id, config):
    with buffer_lock:
        if peer_id not in message_buffer or not message_buffer[peer_id]['messages']:
            return

        digest_data = message_buffer[peer_id]
        del message_buffer[peer_id]

    recipients = config.get(str(peer_id), [])
    if not recipients:
        return

    chat_title = get_chat_title(vk, peer_id)
    msg_count = len(digest_data['messages'])
    start_time = format_timestamp(digest_data['first_time'])

    # --- 1. Генерируем обычный текст (запасной вариант) ---
    plain_lines = [
        f"Дайджест из чата: {chat_title}",
        f"Сообщений: {msg_count} | Начало: {start_time}",
        "=" * 40, ""
    ]

    # --- 2. Генерируем красивый HTML ---
    html_content = f"""
    <div style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f5f7; padding: 20px;">
        <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">

            <!-- Шапка письма -->
            <div style="background-color: #2787F5; color: white; padding: 20px; text-align: center;">
                <h2 style="margin: 0; font-size: 20px;">Дайджест: {chat_title}</h2>
                <p style="margin: 5px 0 0 0; font-size: 14px; opacity: 0.9;">Сообщений: {msg_count} | Начало: {start_time}</p>
            </div>

            <!-- Тело письма -->
            <div style="padding: 20px;">
    """

    all_attachments = []
    total_attach_size = 0

    for i, msg_info in enumerate(digest_data['messages'], 1):
        user = msg_info['user']
        time_str = msg_info['time']
        text = msg_info['text']

        # Экранируем HTML-теги
        safe_text = html.escape(text).replace('\n', '<br>') if text else '<i style="color: #939393;">(без текста)</i>'

        plain_lines.append(f"[{i}] {user} ({time_str}):\n{text if text else '(без текста)'}\n")

        # Формируем блок сообщения
        html_content += f"""
                <div style="margin-bottom: 20px; padding-bottom: 15px; border-bottom: 1px solid #e5e5e5;">
                    <div style="margin-bottom: 8px;">
                        <span style="font-weight: bold; color: #000000; font-size: 15px;">{user}</span>
                        <span style="color: #939393; font-size: 12px; margin-left: 8px;">{time_str}</span>
                    </div>
                    <div style="color: #333333; font-size: 14px; line-height: 1.5; background-color: #f0f2f5; padding: 10px 14px; border-radius: 10px; border-top-left-radius: 0;">
                        {safe_text}
                    </div>
        """

        # Добавляем ссылки
        if msg_info['links']:
            html_content += "<div style='margin-top: 8px; font-size: 13px;'>"
            for link in msg_info['links']:
                html_content += f"<a href='{link}' style='color: #2787F5; text-decoration: none;'>🔗 {link}</a><br>"
                plain_lines.append(f"Ссылка: {link}")
            html_content += "</div>"

        # Собираем вложения
        for fname, fdata in msg_info['attachments']:
            if total_attach_size + len(fdata) <= MAX_TOTAL_ATTACHMENTS:
                all_attachments.append((f"msg{i}_{fname}", fdata))
                total_attach_size += len(fdata)
                html_content += f"<div style='margin-top: 8px; font-size: 13px; color: #555;'>📎 <b>Вложение:</b> {fname}</div>"
                plain_lines.append(f"Вложение: {fname}")
            else:
                html_content += f"<div style='margin-top: 8px; font-size: 13px; color: #d93025;'>⚠️ Вложение {fname} пропущено (превышен лимит)</div>"
                plain_lines.append(f"[Вложение {fname} пропущено]")

        html_content += "</div>"  # Закрываем блок сообщения

    # Закрываем теги HTML
    html_content += """
            </div>
            <div style="background-color: #f4f5f7; text-align: center; padding: 15px; color: #939393; font-size: 12px;">
                Автоматическое сообщение от VK Bot
            </div>
        </div>
    </div>
    """

    subject = f"Дайджест: {chat_title} ({msg_count} сообщ.)"
    send_email_with_attachments(subject, "\n".join(plain_lines), html_content, all_attachments, recipients)


def digest_worker(vk, config):
    """Фоновый поток: проверяет буфер каждые 10 секунд и отправляет по таймеру."""
    while True:
        time.sleep(10)
        now = time.time()
        peers_to_flush = []

        with buffer_lock:
            for peer_id, data in message_buffer.items():
                if now - data['first_time'] >= DIGEST_TIMEOUT_SEC:
                    peers_to_flush.append(peer_id)

        for peer_id in peers_to_flush:
            logging.info(f"Таймер дайджеста сработал для {peer_id}")
            flush_digest(vk, peer_id, config)


def handle_chat_message(vk, msg, config):
    peer_id = msg.get("peer_id")
    from_id = msg.get("from_id")
    recipients = config.get(str(peer_id), [])

    if not recipients:
        send_vk_message(vk, peer_id,
                        f"⚠ Эта беседа ({peer_id}) не подключена к почте. Настройте её в личных сообщениях бота.")
        return

    user_name = get_user_name(vk, from_id)
    sent_at = format_timestamp(msg.get("date"))
    text = msg.get("text", "").strip()
    attachments, links = process_attachments(msg)

    msg_data = {
        'user': user_name,
        'time': sent_at,
        'text': text,
        'attachments': attachments,
        'links': links
    }

    with buffer_lock:
        if peer_id not in message_buffer:
            message_buffer[peer_id] = {'messages': [], 'first_time': time.time()}

        message_buffer[peer_id]['messages'].append(msg_data)
        current_count = len(message_buffer[peer_id]['messages'])

    # Если набралось достаточно сообщений, шлем сразу
    if current_count >= DIGEST_MAX_MESSAGES:
        logging.info(f"Лимит в {DIGEST_MAX_MESSAGES} сообщений достигнут для {peer_id}")
        flush_digest(vk, peer_id, config)


# ==================== АДМИН-ПАНЕЛЬ ====================
def process_admin_message(vk, user_id, peer_id, text, config):
    text = text.strip()

    if text.lower() in ("отмена", "/cancel"):
        user_states.pop(user_id, None)
        send_vk_message(vk, peer_id, "Операция отменена.", get_main_keyboard())
        return

    if text in ("Начать", "/start", "/menu", "Меню"):
        user_states.pop(user_id, None)
        send_vk_message(vk, peer_id, "Привет! Это панель управления VK Bot.\n\nВыбери нужное действие:",
                        get_main_keyboard())
        return

    if user_id in user_states:
        state_data = user_states[user_id]
        state = state_data["state"]

        if state in ("waiting_chat_id_add", "waiting_chat_id_del"):
            if not text.isdigit() or int(text) < 2000000000:
                send_vk_message(vk, peer_id, "Неверный peer_id. Введи ID беседы ещё раз:", get_cancel_keyboard())
                return
            action = "add" if state == "waiting_chat_id_add" else "del"
            user_states[user_id] = {"state": f"waiting_email_{action}", "chat_id": text}
            send_vk_message(vk, peer_id, "Теперь введи email:", get_cancel_keyboard())
            return

        if state == "waiting_email_add":
            chat_id = state_data["chat_id"]
            email = text.lower()
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                send_vk_message(vk, peer_id, "Неверный email. Попробуй ещё раз:", get_cancel_keyboard())
                return
            config.setdefault(chat_id, [])
            if email in config[chat_id]:
                response = "Этот email уже подключён."
            else:
                config[chat_id].append(email)
                save_config(config)
                response = f"Email {email} успешно добавлен."
            user_states.pop(user_id, None)
            send_vk_message(vk, peer_id, response, get_main_keyboard())
            return

        if state == "waiting_email_del":
            chat_id = state_data["chat_id"]
            email = text.lower()
            if chat_id in config and email in config[chat_id]:
                config[chat_id].remove(email)
                if not config[chat_id]: del config[chat_id]
                save_config(config)
                response = "Email успешно удалён."
            else:
                response = "Такой email не найден."
            user_states.pop(user_id, None)
            send_vk_message(vk, peer_id, response, get_main_keyboard())
            return

    if text in ("Добавить почту", "/add"):
        user_states[user_id] = {"state": "waiting_chat_id_add"}
        send_vk_message(vk, peer_id, "Введи peer_id беседы, в которую нужно подключить email:", get_cancel_keyboard())
    elif text in ("Удалить почту", "/del"):
        user_states[user_id] = {"state": "waiting_chat_id_del"}
        send_vk_message(vk, peer_id, "Введи peer_id беседы:", get_cancel_keyboard())
    elif text in ("Список почт", "/list"):
        if not config:
            send_vk_message(vk, peer_id, "Пока нет подключённых бесед.", get_main_keyboard())
            return
        lines = ["Подключённые беседы:\n"]
        for chat_id, emails in config.items():
            title = get_chat_title(vk, int(chat_id))
            lines.append(f"{title} ({chat_id})")
            for email in emails: lines.append(f"  • {email}")
            lines.append("")
        send_long_vk_message(vk, peer_id, "\n".join(lines))
    elif text == "Статус бота":
        send_vk_message(vk, peer_id,
                        f"Бот запущен.\nПодключено бесед: {len(config)}\nРежим: Дайджест (макс. {DIGEST_MAX_MESSAGES} сообщ. / {DIGEST_TIMEOUT_SEC // 60} мин.)",
                        get_main_keyboard())
    elif text in ("Помощь", "/help"):
        help_text = (
            "ПАНЕЛЬ УПРАВЛЕНИЯ\n\nДобавить почту — подключить email к беседе.\nУдалить почту — отключить email.\nСписок почт — показать все подключения.\n\n"
            "Бот работает в режиме ДАЙДЖЕСТА: он копит сообщения и отправляет их одним письмом, если прошло 15 минут или набралось 15 сообщений."
        )
        send_vk_message(vk, peer_id, help_text, get_main_keyboard())
    else:
        send_vk_message(vk, peer_id, "Выбери действие с помощью кнопок меню.", get_main_keyboard())


# ==================== ЗАПУСК ====================
def main():
    config = load_config()
    vk_session = vk_api.VkApi(token=VK_GROUP_TOKEN)
    vk = vk_session.get_api()

    # Запускаем фоновый поток для таймера
    threading.Thread(target=digest_worker, args=(vk, config), daemon=True).start()

    logging.info("Подключение к VK Long Poll...")

    while True:
        try:
            longpoll = VkBotLongPoll(vk_session, VK_GROUP_ID)
            logging.info("Бот запущен и ожидает сообщения.")

            for event in longpoll.listen():
                if event.type != VkBotEventType.MESSAGE_NEW:
                    continue
                msg = event.object.message
                if msg.get("out") == 1: continue

                peer_id = msg.get("peer_id")
                from_id = msg.get("from_id")
                text = msg.get("text", "").strip()

                # ЛИЧНЫЕ СООБЩЕНИЯ (АДМИНКА)
                if peer_id == from_id:
                    if not is_manager(from_id):
                        send_vk_message(vk, peer_id, f"У вас нет прав администратора.\nВаш ID: {from_id}")
                        continue
                    process_admin_message(vk, from_id, peer_id, text, config)

                # БЕСЕДЫ (БУФЕРИЗАЦИЯ)
                elif peer_id >= 2000000000:
                    handle_chat_message(vk, msg, config)

        except KeyboardInterrupt:
            logging.info("Бот остановлен.")
            break
        except Exception:
            logging.exception("Ошибка Long Poll. Переподключение через 5 секунд.")
            time.sleep(5)


if __name__ == "__main__":
    main()