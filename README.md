# OTP SZÉP Kártya Home Assistant component

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Custom component for [Home Assistant](https://home-assistant.io) that tracks the balance of an OTP SZÉP Kártya.

This is a fork of [ofalvai/home-assistant-szep-kartya](https://github.com/ofalvai/home-assistant-szep-kartya), which has not been updated since January 2023. The fork keeps the component working against the current OTP portal.

![Screenshot](screenshot.png?raw=true)

## What is different from upstream

- **Current portal endpoint.** The balance comes from the portal's quick balance query (`/ajax/gyorsegyenleg/`), which needs the full 16-digit card number. The old `/ajax/egyenleglekerdezes/` endpoint is gone.
- **The sensor survives a failed query.** Upstream ran the first query during platform setup. If the portal answered with anything unexpected, setup failed, Home Assistant never retried it, and the sensor disappeared until the next restart. Here the entity is always created, a failed query is logged, and the previous balance is kept.
- **Timeouts.** Requests to the portal time out after 30 seconds instead of blocking forever.

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

`scan_interval`: Use a few hours instead of the default 30 seconds. The portal asks for a captcha when it is queried too often, and the sensor cannot solve it.

## The sensor

The state is the balance in HUF, taken from the `szamla_osszeg9` field of the portal's response. The same value is also exposed as the `Egyenleg` attribute.

## Troubleshooting

All messages are logged under `custom_components.szep_kartya.sensor`:

| Log message | Meaning |
|---|---|
| `Captcha protection kicked in (too many requests)` | The portal was queried too often. Increase `scan_interval`, the next query usually works. |
| `Wrong card number or card code` | Check `card_number` (16 digits) and `card_code`. |
| `Unexpected balance response (HTTP ...)` | The portal answered with something the component does not know. The start of the response is logged. |
| `Balance update failed: ...` | Network error, or the portal page changed and the `ajax_token` could not be found. |

In every case the entity stays in place: it keeps its previous balance, or stays `unknown` if no query has succeeded since the restart.
