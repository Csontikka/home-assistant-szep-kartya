"""Constants for the SZÉP Kártya integration."""

from datetime import timedelta

DOMAIN = 'szep_kartya'
PLATFORMS = ['sensor', 'binary_sensor']

CONF_CARD_NUMBER = 'card_number'
CONF_CARD_CODE = 'card_code'
CONF_SCAN_HOURS = 'scan_hours'

DEFAULT_NAME = 'SZÉP Kártya'
DEFAULT_SCAN_HOURS = 4
MIN_SCAN_HOURS = 1
MAX_SCAN_HOURS = 24

# Minimum gap between two queries of the same card. A burst of Home Assistant
# restarts sends only one query, because the last attempt is stored.
MIN_QUERY_GAP = timedelta(minutes=15)
# Minimum gap between any two portal queries, across all cards. The portal
# answers frequent queries with captchas and even with card errors for a
# working card.
GLOBAL_QUERY_GAP_SECONDS = 60
# After a captcha or a tolerated card error the next query waits at least this
# long, doubling up to a day.
BACKOFF_START = timedelta(hours=8)
BACKOFF_MAX = timedelta(hours=24)
# No successful query for this long marks the data stale.
STALE_AFTER = timedelta(hours=48)

# 'HI' reasons that blame the card. Retrying a wrong card code risks locking the
# card, so polling stops at the first of these and Home Assistant asks for
# the card code again.
CARD_REASONS = {
    'hibas_kartyaszam_vagy_telekod',
    'letiltott_inaktiv_kartya',
    'nincs_kartya',
    'virtualis_kartya',
}
# The portal has also answered nincs_kartya for a working card right after a
# burst of queries. For a card that had a successful query before, this reason
# only stops polling after REJECTION_LIMIT answers in a row.
TOLERATED_REASONS = {'nincs_kartya'}
REJECTION_LIMIT = 3
CAPTCHA_REASONS = {'hibas_recaptcha'}

# Balance fields of the quick balance answer, as labelled by the portal's own
# balance page (data-data attributes).
POCKET_ACCOMMODATION = 'szamla_osszeg9'  # Szálláshely zseb
POCKET_ACTIVE_HUNGARIANS = 'szamla_osszeg8'  # Aktív Magyarok zseb

STORE_VERSION = 1

ISSUE_DEPRECATED_YAML = 'deprecated_yaml'
ISSUE_INVALID_YAML = 'invalid_yaml'
ISSUE_IMPORT_FAILED = 'import_failed'
