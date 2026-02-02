from asyncio import sleep
from logging import Logger

from rnet import Client

from ._const import (
    _PAGE_URL,
    _QUERY_PARAMS,
    _EDIT_API_URL,
    _MAX_RETRIES,
    _TRANSLATE_API_URL,
    _ORIGIN_URL, _CLIENT,
    _ACTIONS_NAME,
    TransformActions,
    YandexAPIError,
    YandexRequestError,
)
from ._utils import _get_sid, _detect_lang_pair, _smart_split, error_delay


async def _make_yandex_request(client: Client, api_url: str, query: dict, form_data: dict) -> dict:
    """
    выполняет POST-запрос к API Яндекса, автоматически получая и добавляя SID.

    функция-обертка для взаимодействия с внутренними API Яндекс.Редактора и
    Яндекс.Переводчика. она получает сессионный ключ (SID), модифицирует
    параметры запроса и отправляет POST-запрос с необходимыми заголовками.

    Args:
        client (Client): инициализированный экземпляр `rnet.Client`.
        api_url (str): целевой URL API (`EDIT_API_URL` или `TRANSLATE_API_URL`).
        query (dict): словарь с параметрами URL-запроса.
        form_data (dict): словарь с данными формы для тела POST-запроса.

    Returns:
        dict: десериализованный JSON-ответ от API.

    Raises:
        RuntimeError: если не удалось получить SID со страницы Яндекса.
        ValueError: если `api_url` не является одним из поддерживаемых.
        rnet.HTTPStatusError: при получении ответа с кодом ошибки HTTP (4xx или 5xx).
    """
    sid = await _get_sid(client)
    if not sid:
        raise RuntimeError('SID не найден, возможно, Яндекс блокирует запрос с помощью капчи.')
    if api_url == _EDIT_API_URL:
        query = query | {'sid': f'{sid}-00-0'}
    elif api_url == _TRANSLATE_API_URL:
        query = query | {'id': f'{sid}-1-0'}
    else:
        raise ValueError('неподдерживаемый API URL')

    resp = await client.post(
        api_url,
        query=query,
        headers={'origin': _ORIGIN_URL, 'referer': _PAGE_URL},
        form=form_data
    )

    resp.raise_for_status()
    return await resp.json()


async def _yandex_translate(input_text: str, client: Client) -> str:
    """
    асинхронно переводит текст с помощью неофициального API Яндекс.Переводчика.

    эмулирует браузер для получения сеансового ключа (SID) и отправляет
    запрос на внутренний API сервиса для выполнения перевода.

    Args:
        input_text (str): исходный текст для перевода.

    Returns:
        str: переведенный текст в случае успеха.
    """
    source_lang, target_lang = _detect_lang_pair(input_text)

    try:
        query = {
            'srv': 'tr-editor',
            'source_lang': source_lang,
            'target_lang': target_lang,
            'reason': 'type-end',
            'format': 'text',
            'ajax': '1',
        }

        form_data = {
            'text': input_text,
            'options': '4'  # параметр обязателен
        }

        json_resp = await _make_yandex_request(client, _TRANSLATE_API_URL, query, form_data)

        # ответ приходит в виде {'code': 200, 'text': ['Translated text']}
        translated_text_list = json_resp.get('text')
        if translated_text_list and isinstance(translated_text_list, list):
            return ''.join(translated_text_list)

        raise YandexAPIError(f'ОШИБКА: "text" не найден или имеет неверный формат в ответе API:\n{json_resp}\n')

    except Exception as e:
        raise YandexRequestError(f'ОШИБКА при переводе текста: {str(e)}')


async def _translate_chunk_with_retry(
        chunk: str,
        chunk_index: int,
        total_chunks: int,
        max_retries: int = _MAX_RETRIES,
        logger: Logger | None = None
) -> str:
    """
    перевод одного чанка с повторными попытками и экспоненциальной задержкой.
    """
    last_exception: Exception | None = None

    for attempt in range(max_retries):
        try:
            result = await _yandex_translate(chunk, _CLIENT)
            return result
        except (YandexAPIError, YandexRequestError) as e:
            last_exception = e

            delay, error_type = error_delay(e, attempt)

            if attempt < max_retries - 1:
                if logger:
                    logger.warning(
                        f'чанк [{chunk_index + 1}/{total_chunks}]: '
                        f'попытка {attempt + 1}/{max_retries} — {error_type}. '
                        f'повтор через {delay:.1f}с...'
                    )
                await sleep(delay)
            else:
                if logger:
                    logger.error(
                        f'чанк [{chunk_index + 1}/{total_chunks}]: '
                        f'все {max_retries} попыток исчерпаны - {error_type}'
                    )

    raise last_exception  # type: ignore


async def _yandex_translate_batch(
        input_text: str,
        max_retries: int = _MAX_RETRIES,
        logger: Logger | None = None
) -> str:
    """
    перевод текста по чанкам с сохранением прогресса.

    при ошибке конкретного чанка — повторяет только его,
    уже переведённые чанки сохраняются.
    """
    input_chunks = _smart_split(text=input_text, max_length=10_000)
    total_chunks = len(input_chunks)
    result_chunks: list[str] = []

    for chunk_index, chunk in enumerate(input_chunks):
        try:
            result = await _translate_chunk_with_retry(
                chunk=chunk,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                max_retries=max_retries,
                logger=logger
            )
            result_chunks.append(result)

        except (YandexAPIError, YandexRequestError) as e:
            translated_count = len(result_chunks)
            raise type(e)(
                f'{str(e)}\n'
                f'[было переведено {translated_count}/{total_chunks} чанков]'
            ) from e

    return ''.join(result_chunks)


async def _yandex_editor_api(input_text: str, client: Client, action: TransformActions = 'correct') -> str:
    """
    выполняет асинхронный запрос к неофициальному API Яндекс.Редактора для преобразования текста.

    функция-обертка, которая инкапсулирует логику получения сессионного ключа (SID)
    и отправки запроса к API для выполнения различных операций над текстом, таких как
    исправление ошибок, перефразирование или изменение стиля.

    Args:
        input_text (str): исходный текст для преобразования.
        action (TransformActions, optional): тип выполняемого действия над текстом:

            - 'correct': исправление орфографических и пунктуационных ошибок.
            - 'improve': общее улучшение читаемости, стиля и структуры текста.
            - 'rephrase': перефразирование текста для изложения тех же мыслей другими словами.
            - 'simple': упрощение текста, снижение лексической и синтаксической сложности.
            - 'complex': усложнение текста, использование более богатой лексики и сложных конструкций.
            - 'formal': приведение текста к официальному, деловому стилю.
            - 'casual': приведение текста к неофициальному, разговорному стилю.
            - 'translate': перевод между Ru<->En

    Returns:
        str: преобразованный текст в случае успеха.
    """
    source_lang, target_lang = _detect_lang_pair(input_text)
    lang = source_lang
    if action == 'translate':
        lang = target_lang
        action = 'correct'

    try:
        data = {
            'action_type': _ACTIONS_NAME.get(action),
            'targ_lang': lang,
            'src_text': input_text,
        }

        json_resp = await _make_yandex_request(client, _EDIT_API_URL, _QUERY_PARAMS, data)
        result = json_resp.get('result_text')
        if result:
            return result
        raise YandexAPIError(f'ОШИБКА: "text" не найден или имеет неверный формат в ответе API:\n{json_resp}\n')

    except Exception as e:
        raise YandexRequestError(f'ОШИБКА при вызове API: {str(e)}')


async def _editor_chunk_with_retry(
        chunk: str,
        chunk_index: int,
        total_chunks: int,
        action: TransformActions = 'correct',
        max_retries: int = _MAX_RETRIES,
        logger: Logger | None = None
) -> str:
    """
    Обработка одного чанка редактором с повторными попытками и экспоненциальной задержкой.
    """
    last_exception: Exception | None = None

    for attempt in range(max_retries):
        try:
            result = await _yandex_editor_api(chunk, _CLIENT, action)
            return result
        except (YandexAPIError, YandexRequestError) as e:
            last_exception = e

            delay, error_type = error_delay(e, attempt)

            if attempt < max_retries - 1:
                if logger:
                    logger.warning(
                        f'чанк [{chunk_index + 1}/{total_chunks}]: '
                        f'попытка {attempt + 1}/{max_retries} — {error_type}. '
                        f'повтор через {delay:.1f}с...'
                    )
                await sleep(delay)
            else:
                if logger:
                    logger.error(
                        f'чанк [{chunk_index + 1}/{total_chunks}]: '
                        f'все {max_retries} попыток исчерпаны - {error_type}'
                    )

    raise last_exception  # type: ignore


async def _yandex_editor_batch(
        input_text: str,
        action: TransformActions = 'correct',
        max_retries: int = _MAX_RETRIES,
        logger: Logger | None = None
) -> str:
    """
    Обработка текста редактором по чанкам с сохранением прогресса.

    При ошибке конкретного чанка — повторяет только его,
    уже обработанные чанки сохраняются.
    """
    input_chunks = _smart_split(text=input_text, max_length=10_000)
    total_chunks = len(input_chunks)
    result_chunks: list[str] = []

    for chunk_index, chunk in enumerate(input_chunks):
        try:
            result = await _editor_chunk_with_retry(
                chunk=chunk,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                action=action,
                max_retries=max_retries,
                logger=logger
            )
            result_chunks.append(result)

        except (YandexAPIError, YandexRequestError) as e:
            processed_count = len(result_chunks)
            raise type(e)(
                f'{str(e)}\n'
                f'[было обработано {processed_count}/{total_chunks} чанков]'
            ) from e

    return ''.join(result_chunks)
