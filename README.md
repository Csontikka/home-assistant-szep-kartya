# OTP SZÉP Kártya Home Assistant component

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Custom component for [Home Assistant](https://home-assistant.io) that tracks the balance of an OTP SZÉP Kártya.

This is a fork of [ofalvai/home-assistant-szep-kartya](https://github.com/ofalvai/home-assistant-szep-kartya), which has not been updated since January 2023. The fork keeps the component working against the current OTP portal.

![Screenshot](screenshot.png?raw=true)

## What is different from upstream

- **Current portal endpoint.** The balance comes from the portal's quick balance query (`/ajax/gyorsegyenleg/`), which needs the full 16-digit card number. The old `/ajax/egyenleglekerdezes/` endpoint is gone.
- **Both pockets.** A separate sensor for the Aktív Magyarok pocket, from the same query.
- **The sensor survives a failed query.** Upstream ran the first query during platform setup. If the portal answered with anything unexpected, setup failed, Home Assistant never retried it, and the sensor disappeared until the next restart. Here the entities are always created, a failed query keeps the previous balance, and the last balance is restored after a restart.
- **Portal errors are understood.** A rejected card stops polling and raises a repair issue, so a wrong card code is not retried until the card gets locked. A captcha makes the next query wait (8 hours, doubling up to a day). Temporary portal outages are just logged.
- **Safer handling of the card data.** An invalid `card_number` or `card_code` is reported in the log and as a repair issue without the value. Numbers are masked in logged responses, the unique ID only uses the last 4 digits of the card, and requests do not follow redirects.
- **Proper sensor entities.** Monetary device class with long-term statistics, and a unique ID, so the sensors can be renamed in the UI.
- **Defaults and limits.** Polling defaults to every 4 hours instead of 30 seconds, requests time out after 30 seconds, and responses are capped in size. No extra Python requirements.

## Installation

1. Install [HACS](https://hacs.xyz/)
2. Add this repository to HACS as a custom repository of type *Integration*: `https://github.com/Csontikka/home-assistant-szep-kartya`
3. Install *OTP SZÉP Kártya* from HACS
4. Add the YAML config to `configuration.yaml` (see below)
5. Restart Home Assistant

### Switching from the upstream repository

1. Note your YAML config, it is not touched by HACS.
2. In HACS, remove the `ofalvai/home-assistant-szep-kartya` custom repository first.
3. Add this repository and download the latest release.
4. Check that `custom_components/szep_kartya/manifest.json` shows this repository in `documentation` and the new version.
5. If `card_number` still holds only the last 8 digits, change it to the full 16-digit number.
6. Restart Home Assistant.

Removing the old repository after downloading the new one can delete the freshly downloaded files, because both install into the same `custom_components/szep_kartya` folder. That is why the order above matters.

## Configuration

``` yaml
sensor:
  - platform: szep_kartya
    card_number: !secret szep_kartya_card_number
    card_code: !secret szep_kartya_card_code
    name: SZÉP Kártya
    scan_interval:
      hours: 4
```

`card_number`: The full 16-digit card number. Quote it in `secrets.yaml` so leading zeroes are kept.

`card_code`: "Telekód" (by default the last 3 digits of the card number). Quote it as well.

`name` (optional): Friendly name of the sensor.

`scan_interval` (optional): Defaults to 4 hours. Do not go much lower: the portal asks for a captcha when it is queried too often, and the sensor cannot solve it.

If `card_number` or `card_code` is invalid, the sensor is not set up, and the log and a repair issue say which one without showing the value. Older versions of this component let Home Assistant put the value into the log in that case, so check old logs before sharing them.

## The sensors

One query per polling round feeds both sensors:

| Sensor | Portal field | Pocket |
|---|---|---|
| `sensor.szep_kartya` (named after `name`) | `szamla_osszeg9` | Szálláshely zseb. Since 2023 the former Vendéglátás and Szabadidő pockets are merged here. |
| `sensor.szep_kartya_aktiv_magyarok` | `szamla_osszeg8` | Aktív Magyarok zseb |

The field mapping comes from the labels on the portal's own balance page. The entity IDs above follow the default `name`.

Attributes:

- `last_success`: time of the last successful query.
- `last_error`: the last error, cleared by the next successful query.
- `stale`: `true` when there was no successful query in the last 48 hours, or none since the restart.
- `Egyenleg` (main sensor only): the balance as text, kept for compatibility with upstream.

A failed query keeps the last balance, and the last balance is restored after a restart, so automations that compare old and new states do not see a fake drop to `unavailable` or `unknown` and back. Use `stale` or `last_success` to notice a long outage. The sensors are only unavailable when the portal rejected the card and there is no balance to show.

`last_success` changes with every successful query. A state trigger without `to:` also fires on attribute changes, so an automation meant for balance changes should compare `trigger.from_state.state` and `trigger.to_state.state`.

## Troubleshooting

All messages are logged under `custom_components.szep_kartya.sensor`:

| Log message | Meaning |
|---|---|
| `Invalid configuration: ...` | `card_number` or `card_code` has the wrong format. Also shown as a repair issue. The sensors are not created until the config is fixed and Home Assistant is restarted. |
| `Captcha protection kicked in ...; next query not before ...` | The portal was queried too often. The next query waits, no action needed unless it keeps happening. |
| `The portal rejected the card (...); polling stopped until Home Assistant restarts` | Also shown as a repair issue. `hibas_kartyaszam_vagy_telekod`: check `card_number` and `card_code`. `letiltott_inaktiv_kartya`: the card is blocked or inactive. Fix the config, then restart. |
| `The portal could not answer the balance query (...)` | Temporary problem on the portal side, e.g. `api_nem_elerheto`. Retried at the next polling round. |
| `Unexpected balance response (HTTP ...)` | The portal answered with something the component does not know. The start of the response is logged, with long numbers masked. |
| `Balance update failed: ...` | Network error, HTTP error, or the portal page changed and the `ajax_token` could not be found. |

In every case the entities stay in place, and the message is also shown in the `last_error` attribute while the sensor is available.
