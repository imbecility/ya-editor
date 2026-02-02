from dataclasses import dataclass
from functools import cache, lru_cache
from json import loads, dumps, JSONDecodeError
from pathlib import Path
from re import search
from tempfile import gettempdir
from time import time
from typing import List, Optional

from rnet import Client

from ._const import _WORK_DIR, _SID_CACHE_FILENAME, _SID_TTL_HOURS, _PAGE_URL, _QUERY_PARAMS, _RETRY_CONFIG, _DEFAULT_RETRY_CONFIG, _BACKOFF_FACTOR


@dataclass
class _ProtectedZone:
    """защищённая зона разметки"""
    start: int
    end: int
    open_tag: str
    close_tag: str
    require_newline: bool = False  # True для CodeBlock (```)
    atomic: bool = False  # True для Link и InlineCode


def _is_escaped(text: str, index: int) -> bool:
    """проверяет, является ли символ на позиции index экранированным"""
    backslashes = 0
    for i in range(index - 1, -1, -1):
        if text[i] == '\\':
            backslashes += 1
        else:
            break
    return backslashes % 2 != 0


def _find_closing_tag(text: str, start_pos: int, tag: str) -> int:
    """
    ищет закрывающий тег, учитывая экранирование.
    возвращает позицию после тега или -1 если не найден.
    """
    search_pos = start_pos
    while True:
        idx = text.find(tag, search_pos)
        if idx == -1:
            return -1
        if not _is_escaped(text, idx):
            return idx + len(tag)
        search_pos = idx + 1


def _find_zones(text: str) -> List[_ProtectedZone]:
    """
    находит все защищённые зоны в тексте (code blocks, inline code, links, formatting)
    """
    zones: List[_ProtectedZone] = []
    n = len(text)
    i = 0

    while i < n:
        if text[i] == '\\':
            i += 2
            continue

        # ```...```
        if text[i:].startswith("```"):
            start = i
            search_pos = i + 3
            end = -1

            while True:
                idx = text.find("```", search_pos)
                if idx == -1:
                    break
                if not _is_escaped(text, idx):
                    end = idx + 3
                    break
                search_pos = idx + 1

            if end != -1:
                # язык (```python)
                newline_idx = text[start:end].find("\n")
                if newline_idx != -1:
                    open_tag = text[start:start + newline_idx]
                else:
                    open_tag = "```"

                zones.append(_ProtectedZone(
                    start=start,
                    end=end,
                    open_tag=open_tag,
                    close_tag="```",
                    require_newline=True,
                    atomic=False
                ))
                i = end
                continue

        # `...`
        if text[i] == '`':
            start = i
            end = _find_closing_tag(text, i + 1, "`")
            if end != -1:
                zones.append(_ProtectedZone(
                    start=start,
                    end=end,
                    open_tag="`",
                    close_tag="`",
                    atomic=True
                ))
                i = end
                continue

        # [text](url)
        if text[i] == '[':
            start = i
            text_end = _find_closing_tag(text, i + 1, "]")

            if text_end != -1 and text_end < n and text[text_end] == '(':
                url_end = _find_closing_tag(text, text_end + 1, ")")
                if url_end != -1:
                    zones.append(_ProtectedZone(
                        start=start,
                        end=url_end,
                        open_tag="",
                        close_tag="",
                        atomic=True
                    ))
                    i = url_end
                    continue

        # *, _, ~, ||, __
        char = text[i]
        tag = ""

        if char == '*':
            tag = "*"
        elif char == '_':
            if i + 1 < n and text[i + 1] == '_':
                tag = "__"
            else:
                tag = "_"
        elif char == '~':
            tag = "~"
        elif char == '|' and i + 1 < n and text[i + 1] == '|':
            tag = "||"

        if tag:
            start = i
            search_start = i + len(tag)
            end = _find_closing_tag(text, search_start, tag)
            if end != -1:
                zones.append(_ProtectedZone(
                    start=start,
                    end=end,
                    open_tag=tag,
                    close_tag=tag,
                    atomic=False
                ))
                i = end
                continue

        i += 1

    return zones


@lru_cache(maxsize=128)
def _smart_split(text: str, max_length: int = 4096) -> List[str]:
    """
    разбивает текст на части с сохранением Markdown разметки.

    Args:
        text: исходный текст с Markdown разметкой
        max_length: максимальная длина одного чанка (по умолчанию 4096)

    Returns:
        список строк-чанков
    """
    if max_length <= 0:
        max_length = 4096

    chunks: List[str] = []
    zones = _find_zones(text)

    current_pos = 0
    pending_prefix = ""
    text_len = len(text)

    while current_pos < text_len:
        effective_max = max_length - len(pending_prefix)
        if effective_max <= 0:
            effective_max = 100  # защита от бесконечного цикла
        if text_len - current_pos <= effective_max:
            chunks.append(pending_prefix + text[current_pos:])
            break

        split_pos = current_pos + effective_max

        while (split_pos > current_pos and
               text[split_pos - 1] == '\\' and
               not _is_escaped(text, split_pos - 1)):
            split_pos -= 1

        active_zone: Optional[_ProtectedZone] = None
        for z in zones:
            if z.start < split_pos < z.end:
                active_zone = z
                break

        suffix = ""
        next_prefix = ""

        if active_zone is not None:
            if active_zone.atomic:
                if active_zone.start > current_pos:
                    split_pos = active_zone.start
                    active_zone = None
            else:
                if active_zone.require_newline:
                    suffix = "\n" + active_zone.close_tag
                    next_prefix = active_zone.open_tag + "\n"
                else:
                    suffix = active_zone.close_tag
                    next_prefix = active_zone.open_tag
                if split_pos + len(suffix) > current_pos + effective_max:
                    split_pos -= len(suffix)

        best_split = split_pos
        found_nice_split = False

        search_back_limit = max(split_pos - 150, current_pos)

        for i in range(split_pos, search_back_limit, -1):
            if text[i - 1] == '\\' and not _is_escaped(text, i - 1):
                continue

            c = text[i - 1]
            if c == '\n':
                best_split = i
                found_nice_split = True
                break
            if c == ' ' and not found_nice_split:
                best_split = i
                found_nice_split = True

        if found_nice_split:
            split_pos = best_split

        chunk_text = text[current_pos:split_pos]
        chunks.append(pending_prefix + chunk_text + suffix)

        current_pos = split_pos
        pending_prefix = next_prefix

    return chunks


def _is_writable_directory(path: str | Path) -> bool:
    """
    проверяет, доступна ли директория для записи путем создания временного файла.

    Args:
        path (str | Path): путь к директории для проверки.

    Returns:
        bool: True, если директория существует и доступна для записи, иначе False.
    """
    directory = Path(path)
    if not directory.is_dir():
        return False
    try:
        test_file = directory / '.write_test'
        test_file.touch(exist_ok=False)
        test_file.unlink()
        return True
    except:  # noqa
        return False


@cache
def _get_sid_file_path() -> Path:
    """
    формирует и кэширует путь к файлу кэша SID.

    декоратор @cache гарантирует, что вычисление пути происходит только один раз.
    путь определяется на основе константы WORK_DIR, если директория доступна для записи.
    в противном случае используется системная временная папка.

    Returns:
        Path: полный путь к файлу кэша SID.
    """
    temp_folder = _WORK_DIR if _is_writable_directory(_WORK_DIR) else Path(gettempdir())
    return temp_folder / _SID_CACHE_FILENAME


def _read_sid_cache() -> dict[str, str] | None:
    cache_path = _get_sid_file_path()
    if cache_path.is_file():
        try:
            data = loads(cache_path.read_text(encoding='utf-8'))
            if data.get('sid') and data.get('timestamp'):
                return data
        except (JSONDecodeError, IOError) as e:
            print(e)
    return None


def _write_sid_cache(sid: str) -> bool:
    cache_path = _get_sid_file_path()
    try:
        data = dumps({'sid': sid, 'timestamp': time()}, ensure_ascii=False)
        cache_path.write_text(data, encoding='utf-8')
        return True
    except Exception as e:
        print(e)
    return False


def _decode_sid(sid: str) -> str:
    """
    декодирует SID, полученный со страницы Яндекс.Редактора
    """
    if not sid:
        return ''
    parts = sid.split('.')
    parts = [part[::-1] for part in parts]
    return '.'.join(parts)


async def _get_sid(client: Client) -> str | None:
    """
    получает и декодирует уникальный SID со страницы Яндекс.Редактора,
    используя rnet для надежной эмуляции браузера.

    Args:
        client: инициализированный rnet.Client с эмуляцией.
    """
    cached_data = _read_sid_cache()
    if cached_data:
        sid = cached_data.get('sid')
        timestamp = cached_data.get('timestamp', 0)
        sid_ttl_seconds = _SID_TTL_HOURS * 60 * 60
        if sid and (time() - timestamp) < sid_ttl_seconds:
            return sid

    resp = await client.get(_PAGE_URL, query=_QUERY_PARAMS)

    resp.raise_for_status()

    html = await resp.text()
    match = search(r'"SID":"([a-z0-9.]+)"', html)

    if not match:
        return None

    new_sid = _decode_sid(match.group(1))

    _write_sid_cache(new_sid)

    return new_sid


@lru_cache(maxsize=128)
def _detect_lang_pair(text: str) -> tuple[str | None, str]:
    """
    определяет доминирующий язык (ru/en) в тексте по количеству символов.

    при равенстве кириллических и латинских символов предпочтение отдается русскому языку.

    Args:
        text (str): входная строка для анализа языка.

    Returns:
        tuple[str, str]: кортеж с парой языков, где первый элемент - определенный язык,
            а второй - парный ему ('ru', 'en' или 'en', 'ru').

    Raises:
        ValueError: если входная строка пуста или не является строкой.
    """
    if not text or not isinstance(text, str):
        raise ValueError('ОШИБКА: входной текст должен быть не пустой строкой!')

    cyrillic_count = 0
    latin_count = 0

    for char in text.lower():
        if 'а' <= char <= 'я' or char == 'ё':
            cyrillic_count += 1
        elif 'a' <= char <= 'z':
            latin_count += 1
    if cyrillic_count > latin_count:
        lang = 'ru'
    elif latin_count > cyrillic_count:
        lang = 'en'
    else:
        lang = 'ru'

    return lang, 'en' if lang == 'ru' else 'ru'


def error_delay(exception: Exception, attempt: int) -> tuple[float, str]:
    config = _RETRY_CONFIG.get(type(exception), _DEFAULT_RETRY_CONFIG)

    delay = min(
        config['base_delay'] * (_BACKOFF_FACTOR ** attempt),
        config['max_delay']
    )

    error_type = type(exception).__name__

    return delay, error_type
