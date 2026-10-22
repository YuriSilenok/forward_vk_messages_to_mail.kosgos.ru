import os
import tempfile
import requests
import smtplib
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
import vk_api
from vk_api.bot_longpoll import VkBotLongPoll, VkBotEventType

# ==================== НАСТРОЙКИ ====================
VK_GROUP_TOKEN = '???'
VK_GROUP_ID = 000

SMTP_SERVER = 'mail.kosgos.ru'
SMTP_PORT = 465
SMTP_USER = '???@kosgos.ru'
SMTP_PASSWORD = '???'

MAIL_TO = '???@kosgos.ru'
MAIL_FROM_NAME = 'VK Bot'
# ===================================================


def get_chat_title(vk, peer_id):
    """Возвращает название беседы VK по peer_id."""
    if peer_id < 2000000000:
        return f"Личный диалог {peer_id}"

    try:
        response = vk.messages.getConversationsById(peer_ids=peer_id)
        items = response.get('items', [])
        if items:
            chat_settings = items[0].get('chat_settings', {})
            title = chat_settings.get('title')
            if title:
                return title
    except Exception as e:
        print(f"[WARN] Не удалось получить название беседы {peer_id}: {e}")

    return f"Беседа {peer_id}"


def resolve_video_url(vk, video, owner_id):
    """
    Пытается получить прямую ссылку на видео через video.get.
    Возвращает URL или None.
    """
    try:
        result = vk.video.get(
            videos=f"{owner_id}_{video['id']}",
            access_key=video.get('access_key')
        )
        items = result.get('items', [])
        if items:
            files = items[0].get('files', {})
            for quality in ('mp4_1080', 'mp4_720', 'mp4_480', 'mp4_360', 'mp4_240'):
                if files.get(quality):
                    return files[quality]
            if files.get('external'):
                return files['external']
    except Exception as e:
        print(f"[WARN] Не удалось получить URL видео: {e}")
    return None


def get_attachment_urls(vk, msg):
    """
    Извлекает вложения из сообщения VK.
    Возвращает список кортежей:
      - (filename, url) для скачиваемых файлов
      - ('__LINK__', url, title) для ссылок (видео без прямой ссылки)
    """
    attachments = msg.get('attachments', [])
    items = []
    for att in attachments:
        att_type = att.get('type')
        if att_type == 'photo':
            sizes = att['photo'].get('sizes', [])
            if sizes:
                best = max(sizes, key=lambda s: s.get('width', 0) * s.get('height', 0))
                if best.get('url'):
                    items.append(('photo_' + str(att['photo']['id']) + '.jpg', best['url']))
        elif att_type == 'doc':
            doc = att['doc']
            url = doc.get('url')
            if url:
                items.append((doc.get('title', 'document'), url))
            else:
                print(f"[SKIP] Документ без URL: {doc.get('title')}")
        elif att_type == 'video':
            video = att['video']
            title = video.get('title', 'video')
            url = video.get('url')
            if not url:
                owner_id = video.get('owner_id')
                url = resolve_video_url(vk, video, owner_id)
            if url:
                items.append((title + '.mp4', url))
            else:
                # Прямой ссылки нет — формируем публичную ссылку на страницу видео
                owner_id = video.get('owner_id')
                video_id = video.get('id')
                page_url = f"https://vk.com/video{owner_id}_{video_id}"
                items.append(('__LINK__', page_url, title))
                print(f"[LINK] Видео доступно только по ссылке: {title} -> {page_url}")
        elif att_type == 'audio':
            audio = att['audio']
            url = audio.get('url')
            if url:
                name = f"{audio.get('artist', 'audio')} - {audio.get('title', 'track')}.mp3"
                items.append((name, url))
        elif att_type == 'wall':
            print("[SKIP] Вложение типа 'wall' пока не поддерживается")
        else:
            print(f"[SKIP] Неподдерживаемый тип вложения: {att_type}")
    return items


def download_file(url):
    """Скачивает файл по URL и возвращает (filename, bytes)."""
    if not url or not str(url).startswith(('http://', 'https://')):
        raise ValueError(f"Некорректный URL: {url!r}")
    resp = requests.get(url, stream=True, timeout=60)
    resp.raise_for_status()
    filename = 'attachment'
    cd = resp.headers.get('Content-Disposition', '')
    if 'filename=' in cd:
        filename = cd.split('filename=')[-1].strip('"\'')
    else:
        url_path = url.split('?')[0].rstrip('/')
        if '/' in url_path:
            filename = url_path.rsplit('/', 1)[-1] or filename
    return filename, resp.content


def format_timestamp(ts):
    """Преобразует Unix timestamp в читаемую дату и время."""
    if not ts:
        return "(время неизвестно)"
    try:
        dt = datetime.fromtimestamp(int(ts))
        return dt.strftime('%d.%m.%Y %H:%M:%S')
    except Exception:
        return str(ts)


def send_email_with_attachments(subject, body, attachments):
    """attachments: список кортежей (filename, bytes)"""
    msg = EmailMessage()
    msg['Subject'] = subject
    msg['From'] = formataddr((MAIL_FROM_NAME, SMTP_USER))
    msg['To'] = MAIL_TO
    msg.set_content(body, charset='utf-8')

    for filename, data in attachments:
        ext = filename.lower().rsplit('.', 1)[-1] if '.' in filename else ''
        maintype, subtype = 'application', 'octet-stream'
        if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
            maintype, subtype = 'image', ext if ext != 'jpg' else 'jpeg'
        elif ext in ('mp4', 'mov', 'avi', 'mkv'):
            maintype, subtype = 'video', ext
        elif ext == 'pdf':
            maintype, subtype = 'application', 'pdf'
        elif ext == 'mp3':
            maintype, subtype = 'audio', 'mpeg'

        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.send_message(msg)
    print(f"[OK] Письмо отправлено: {subject} (вложений: {len(attachments)})")


def main():
    vk_session = vk_api.VkApi(token=VK_GROUP_TOKEN)
    longpoll = VkBotLongPoll(vk_session, VK_GROUP_ID)
    vk = vk_session.get_api()

    print("Бот запущен. Слушаю сообщения...")

    while True:
        try:
            for event in longpoll.listen():

                print(f"Событие: {event.type}")

                if event.type != VkBotEventType.MESSAGE_NEW:
                    continue

                msg = event.object.message

                if msg.get('out') == 1:
                    continue

                peer_id = msg.get('peer_id')
                from_id = msg.get('from_id')
                text = msg.get('text', '').strip()
                date_ts = msg.get('date')

                # Кто
                try:
                    user = vk.users.get(user_ids=from_id)[0]
                    user_name = f"{user['first_name']} {user['last_name']}"
                except Exception:
                    user_name = f"ID {from_id}"

                # Когда
                sent_at = format_timestamp(date_ts)

                # Название чата
                chat_title = get_chat_title(vk, peer_id)

                # Собираем вложения и ссылки
                attachments_data = []
                link_lines = []  # строки со ссылками для тела письма

                for item in get_attachment_urls(vk, msg):
                    # Ссылка на видео (не скачивается)
                    if item[0] == '__LINK__':
                        _, link_url, link_title = item
                        link_lines.append(f"  {link_title}: {link_url}")
                        continue

                    # Обычное вложение — скачиваем
                    filename, url = item
                    try:
                        fname, data = download_file(url)
                        if fname == 'attachment':
                            fname = filename
                        attachments_data.append((fname, data))
                        print(f"  Скачан файл: {fname} ({len(data)} байт)")
                    except Exception as e:
                        print(f"  Ошибка скачивания {url}: {e}")

                # Тело письма: кто, когда, что
                body_lines = [
                    f"Кто: {user_name}",
                    f"Когда: {sent_at}",
                    "",
                    "Что:",
                    text if text else "(без текста)"
                ]

                # Добавляем ссылки на видео, если есть
                if link_lines:
                    body_lines.append("")
                    body_lines.append("Ссылки на видео:")
                    body_lines.extend(link_lines)

                body = "\n".join(body_lines)

                # Тема письма — только название чата
                subject = chat_title

                try:
                    send_email_with_attachments(subject, body, attachments_data)
                except Exception as e:
                    print(f"[ERROR] Не удалось отправить письмо: {e}")
        except Exception as ex:
            print(ex)


if __name__ == '__main__':
    main()