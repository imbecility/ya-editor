from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from rnet_client import get_rnet_client

class YandexAPIError(Exception):
    pass

class YandexRequestError(Exception):
    pass

SupportedLanguages = Literal['ru', 'en']
TransformActions = Literal['complex', 'simple', 'formal', 'casual', 'rephrase', 'improve', 'correct', 'translate']

_CLIENT = get_rnet_client()
_ACTIONS_NAME = {
    'complex': 'make_text_more_complex',
    'simple': 'make_text_more_simple',
    'formal': 'make_text_more_formal',
    'casual': 'make_more_casual',
    'rephrase': 'rephrase',
    'improve': 'improve_text',
    'correct': 'correct_mistakes',
}

_QUERY_PARAMS = {'srv': 'tr-editor'}
_PAGE_URL = 'https://translate.yandex.ru/editor'
_ORIGIN_URL = f'{urlparse(_PAGE_URL).scheme}://{urlparse(_PAGE_URL).netloc}'

_EDIT_API_URL = 'https://translate.yandex.ru/editor/api/v1/transform-text'
_TRANSLATE_API_URL = 'https://translate.yandex.net/api/v1/tr.json/translate'

_WORK_DIR = Path(__file__).parent
_SID_CACHE_FILENAME = 'yandex_editor_sid.json'
_SID_TTL_HOURS = 12


_MAX_RETRIES = 3
_BACKOFF_FACTOR = 2.0

_RETRY_CONFIG = {
    YandexAPIError: {
        'base_delay': 1.0,
        'max_delay': 10.0,
    },
    YandexRequestError: {
        'base_delay': 2.0,
        'max_delay': 30.0,
    },
}

_DEFAULT_RETRY_CONFIG = {'base_delay': 1.0, 'max_delay': 15.0}