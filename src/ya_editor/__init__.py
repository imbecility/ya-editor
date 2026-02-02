from logging import Logger

from ._const import TransformActions
from ._core import _yandex_translate_batch, _yandex_editor_batch


async def translate(input_text: str, logger: Logger | None = None) -> str:
    """
    асинхронно переводит текст с помощью неофициального API Яндекс.Переводчика.

    эмулирует браузер для получения сеансового ключа (SID) и отправляет
    запрос на внутренний API сервиса для выполнения перевода.

    Args:
        input_text (str): исходный текст для перевода.

        logger (logging.Logger, optional): настроенный стандартный логгер

    Returns:
        str: переведенный текст в случае успеха.
    """
    return await _yandex_translate_batch(input_text, logger=logger)


async def transform(input_text: str, action: TransformActions = 'correct', logger: Logger | None = None) -> str:
    """
    выполняет асинхронный запрос к неофициальному API Яндекс.Редактора для преобразования текста.

    Args:
        input_text (str): исходный текст для преобразования.

        action (TransformActions, optional): тип выполняемого действия над текстом:

            - 'correct': исправление орфографических, типографических и пунктуационных ошибок.
            - 'improve': общее улучшение читаемости, стиля и структуры текста.
            - 'rephrase': перефразирование текста для изложения тех же мыслей другими словами.
            - 'simple': упрощение текста, снижение лексической и синтаксической сложности.
            - 'complex': усложнение текста, использование более богатой лексики и сложных конструкций.
            - 'formal': приведение текста к официальному, деловому стилю.
            - 'casual': приведение текста к неофициальному, разговорному стилю.
            - 'translate': перевод между Ru<->En

        logger (logging.Logger, optional): настроенный стандартный логгер

    Returns:
        str: преобразованный текст в случае успеха.
    """
    return await _yandex_editor_batch(input_text, action=action, logger=logger)


__all__ = [
    'translate',
    'transform',
    'TransformActions'
]
