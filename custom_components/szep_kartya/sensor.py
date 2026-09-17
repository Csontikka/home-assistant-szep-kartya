import logging
from homeassistant.helpers.entity import Entity
import voluptuous as vol
import homeassistant.helpers.config_validation as cv
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import CONF_NAME

import requests
from bs4 import BeautifulSoup as bs
import re
import json

_LOGGER = logging.getLogger(__name__)

DOMAIN = 'szep_kartya'

CONF_CARD_NUMBER = 'card_number'
CONF_CARD_CODE = 'card_code'
CONF_MAIN_BALANCE = 'main_balance'
CONF_BALANCE = 'egyenleg'
CONF_BALANCE_VALUES = [CONF_BALANCE]


DEFAULT_NAME = 'SZÉP Kártya'
DEFAULT_UNIT = 'Ft'
DEFAULT_ICON = 'mdi:credit-card-outline'

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_CARD_NUMBER): vol.All(cv.string, vol.Length(min=16, max=16)),
        vol.Required(CONF_CARD_CODE): vol.All(cv.string, vol.Length(min=3, max=3)),
        vol.Optional(CONF_NAME, DEFAULT_NAME): cv.string
    }
)

URL_API = 'https://magan.szepkartya.otpportalok.hu/ajax/gyorsegyenleg/'
URL_HTML = 'https://magan.szepkartya.otpportalok.hu/fooldal/'
REQUEST_TIMEOUT = 30


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    card_number = config.get(CONF_CARD_NUMBER)
    card_code = config.get(CONF_CARD_CODE)
    main_balance = config.get(CONF_MAIN_BALANCE)
    name = config.get(CONF_NAME)

    sensor = SzepKartyaSensor(card_number, card_code, main_balance, name)

    # Add the entity first and let HA run the first update. A failed fetch
    # used to abort platform setup, and HA never retries that.
    async_add_entities([sensor], True)


class SzepKartyaSensor(Entity):
    def __init__(self, card_number: int, card_code: int, main_balance: str, name: str):
        self._state = None
        self.balance = None

        self.card_number: int = card_number
        self.card_code: int = card_code
        self.main_balance: str = main_balance
        self._name: str = name

        self.token: str = ''
        self.session_id: str = ''

    @property
    def name(self):
        return self._name

    @property
    def state(self):
        return self.balance

    @property
    def extra_state_attributes(self):
        return {
            'Egyenleg': f'{self.balance} {DEFAULT_UNIT}'
        }

    @property
    def unit_of_measurement(self):
        return DEFAULT_UNIT

    @property
    def icon(self):
        return DEFAULT_ICON

    def update(self):
        # Never raise: an exception in the first update makes HA drop the
        # entity. On failure the previous balance is kept.
        try:
            self.scrape_tokens()
            self.fetch_balance()
        except Exception as err:
            _LOGGER.error('Balance update failed: %r', err)
        self._state = self.balance

    def scrape_tokens(self):
        response_html = requests.get(URL_HTML, timeout=REQUEST_TIMEOUT)
        soup = bs(response_html.text, 'html.parser')
        script_tag = soup.find('script', string=re.compile('ajax_token'))
        if script_tag is None:
            raise ValueError('Can\'t find the ajax_token script tag (HTTP %s)' % response_html.status_code)
        script_tag_text = script_tag.string

        match = re.search(r'ajax_token = \'([a-z0-9]{64})\'', script_tag_text)
        if not match:
            raise ValueError('Can\'t find ajax_token in script tag')

        self.token = match.group(1)
        self.session_id = response_html.cookies['PHPSESSID']

    def fetch_balance(self):
        request_body = f's_azonosito_k={self.card_number}&s_telekod_k={self.card_code}&ajax_token={self.token}&s_captcha='
        cookies = dict(PHPSESSID=self.session_id)
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8'
        }
        response_api = requests.post(URL_API, headers=headers, data=request_body, cookies=cookies,
                                     timeout=REQUEST_TIMEOUT)

        response_json = json.loads(response_api.text)
        first = response_json[0] if isinstance(response_json, list) and response_json else response_json
        if first == 'RC':
            _LOGGER.error('Captcha protection kicked in (too many requests)')
        elif first == 'HI':
            _LOGGER.error('Wrong card number or card code')
        elif isinstance(first, dict) and isinstance(first.get('UZENET'), dict):
            self.balance = parse_balance(first['UZENET']['szamla_osszeg9'])
        else:
            _LOGGER.error('Unexpected balance response (HTTP %s): %.200s',
                          response_api.status_code, response_api.text)

def parse_balance(input_string: str) -> int:
    if input_string.strip() == '':
        return 0
    else:
        input_clean: str = input_string.strip().replace('+', '')
        return int(input_clean)
