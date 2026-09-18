# OTP SZÉP Kártya – Home Assistant integráció

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Az OTP SZÉP Kártya egyenlegét mutatja Home Assistantban, zsebenként. Több kártyát is kezel, és a beállítás a felületen történik.

Ez az [ofalvai/home-assistant-szep-kartya](https://github.com/ofalvai/home-assistant-szep-kartya) forkja. Az eredeti 2023 januárja óta nem változott; a fork a mai OTP portálhoz igazodik.

![Képernyőkép](screenshot.png?raw=true)

## Telepítés

1. Telepítsd a [HACS](https://hacs.xyz/)-t.
2. HACS › *Custom repositories*: `https://github.com/Csontikka/home-assistant-szep-kartya`, típus: *Integration*.
3. Telepítsd a *SZÉP Kártya* integrációt, majd indítsd újra a Home Assistantot.
4. *Beállítások › Eszközök és szolgáltatások › Integráció hozzáadása › SZÉP Kártya*.

Kártyánként add meg:

- **Név**: az eszköz neve, például a kártya tulajdonosa. Ebből lesznek az entitásnevek.
- **Kártyaszám**: a teljes, 16 jegyű szám (szóköz és kötőjel megengedett).
- **Telekód**: háromjegyű, alapértelmezetten a kártyaszám utolsó 3 számjegye.

Hozzáadáskor egyszer lekérdezzük az egyenleget, így azonnal kiderül, ha valami el van gépelve. Ugyanazt a kártyát kétszer nem lehet felvenni, és két olyan kártyát sem, amelynek egyezik az utolsó 4 számjegye (az entitásazonosítók ebből képződnek).

**Beállítások** (az integráció *Konfigurálás* gombja): a lekérdezés gyakorisága, 1 és 24 óra között, alapból 4 óra.

## Entitások kártyánként

| Entitás | Mit mutat |
|---|---|
| Szálláshely zseb | A `szamla_osszeg9` mező. 2023 óta a korábbi Vendéglátás és Szabadidő zseb is ide van összevonva. |
| Aktív Magyarok zseb | A `szamla_osszeg8` mező. |
| Összesen | A két zseb együtt. |
| Utolsó sikeres lekérdezés | Diagnosztikai időbélyeg. |
| Lekérdezési probléma | Diagnosztikai bináris szenzor: be van kapcsolva, ha a legutóbbi lekérdezés hibás volt, a lekérdezés leállt, vagy 48 órája nincs friss adat. Attribútumai a részletek. |

A mezők jelentése a portál saját egyenleg-oldalának címkéiből származik. A pénzösszeg-szenzoroknak van hosszú távú statisztikája.

A Szálláshely zseb szenzoron megmaradtak az 1.2.x attribútumai (`last_success`, `last_attempt`, `last_error`, `not_before`, `rejections`, `polling_stopped`, `stale`, `Egyenleg`), hogy a meglévő dashboardok és automatizmusok működjenek.

## Hogyan kíméli a portált

A portál a sűrű lekérdezésre captchával válaszol, és ilyenkor működő kártyára is adhat `nincs_kartya` hibát. Ezért:

- Egy kártyát legfeljebb 15 percenként kérdezünk le. A kísérletet még a lekérdezés előtt elmentjük, így egy újraindítás-sorozat egyetlen lekérdezés, akkor is, ha az újraindítás épp lekérdezés közben jön. A 15 percen belüli kézi frissítés nem csinál semmit.
- A kártyák lekérdezései sorban mennek, köztük legalább 1 perc szünettel.
- Minden kör ±10 percet csúszik véletlenszerűen, így a portál nem másodpercre ugyanakkor kapja a kérést minden nap, és két Home Assistant sem sodródhat egymásra.
- Captcha után a következő lekérdezés vár: 8 óra, ismétlődésnél duplázódva legfeljebb 24 óra. Ez **minden kártyára** vonatkozik, mert a korlát a közös IP-címet éri.
- Egy sikertelen lekérdezés nem nullázza az egyenleget, és nem teszi elérhetetlenné a szenzort. Az automatizmusok így nem látnak hamis költést vagy jóváírást.

## Ha a portál elutasítja a kártyát

- `hibas_kartyaszam_vagy_telekod`, `letiltott_inaktiv_kartya`, `virtualis_kartya`: a lekérdezés azonnal leáll, hogy egy rossz telekód ne zárolja a kártyát. A Home Assistant értesítést küld, és a felületen kéri újra a telekódot, újraindítás után is. Mentés előtt egyszer lekérdezzük az egyenleget.
- `nincs_kartya`: ha a kártya korábban már működött, átmenetinek vesszük (várakozással), és csak 3 egymás utáni elutasításnál áll le. Ha még sosem működött, azonnal leáll.
- Minden más portálhiba (pl. `api_nem_elerheto`) átmeneti: naplózzuk, és a következő körben újrapróbáljuk.

A kártyaszám és a telekód nem kerül a naplóba, és a diagnosztikai letöltésből is ki van takarva. A Home Assistant a felületen felvett adatokat a saját tárolójában (`.storage`) tartja, ugyanúgy titkosítatlanul, mint a `secrets.yaml`-t.

## Átállás a régi YAML-konfigurációról

Nincs teendő a frissítés előtt. Az első indításkor a `sensor:` alatti `platform: szep_kartya` blokk magától átkerül a felületre:

- az entitásazonosítók (pl. `sensor.szep_kartya`) és az előzmények megmaradnak, így az automatizmusok változatlanul működnek;
- az utolsó egyenleg és a lekérdezési előzmény is átjön, tehát a frissítés miatti újraindítás nem küld fölösleges lekérdezést;
- a `scan_interval` beállítás lesz a lekérdezés gyakorisága (1 és 24 óra közé kerekítve).

Utána egy javítási értesítés jelzi, hogy a YAML-blokk törölhető. A YAML-ből már nem jönnek létre entitások.

A szenzorok megjelenített neve a felületes szerkezethez igazodik (pl. *SZÉP Kártya Szálláshely zseb*); az entitásazonosító és az ikon nem változik.

Az átállás után a YAML-ban vagy a `secrets.yaml`-ben átírt telekód már nem számít: a telekódot a felületen lehet cserélni (elutasításkor a Home Assistant magától kéri).

## Hibaelhárítás

A naplóüzenetek a `custom_components.szep_kartya` alatt jelennek meg, angolul:

| Üzenet | Jelentés |
|---|---|
| `Captcha protection kicked in ...; next query not before ...` | A portál sok kérést kapott. A következő lekérdezés vár, nincs teendő, hacsak nem ismétlődik tartósan. |
| `The portal rejected the card (nincs_kartya), 1 of 3 in a row; ...` | Korábban működő kártya, átmenetinek vesszük. |
| `The portal rejected the card (...); polling stopped until the card code is entered again` | Az értesítésben add meg újra a telekódot. |
| `The portal could not answer the balance query (...)` | Átmeneti portálhiba, a következő körben újrapróbáljuk. |
| `Unexpected balance response (HTTP ...)` | Ismeretlen válasz; a válasz eleje naplózva, a számok kitakarva. |
| `Balance update failed: ...` | Hálózati vagy HTTP-hiba, vagy megváltozott a portál oldala. |

## Fejlesztés

A tesztek a Home Assistant 2026.9.2 tesztkörnyezetében futnak (Python 3.14):

```
pip install pytest-homeassistant-custom-component==0.13.365
pytest
```
