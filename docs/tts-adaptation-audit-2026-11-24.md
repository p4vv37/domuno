# Audyt adaptacji `ikidd/pocket-tts-wyoming`

**Data:** 24 listopada 2026  
**Zakres:** `tts-pocket-wyoming/` (kopia w tym repozytorium), `../tts-pocket-wyoming/` (klon roboczy poza repozytorium), historia Git, metadane GitHub i Hugging Face.  
**Konwencja:** `repo` oznacza kopię w tym repozytorium, `klon` — katalog roboczy z klonem upstreamu poza nim.  
**Metoda:** analiza źródeł i diffów; bez zmian runtime, buildów, testów modelu i ruchu na urządzeniu.  
**Status:** audyt zakończony. **Dalsza publikacja adaptowanego serwera powinna być wstrzymana do wyjaśnienia licencji upstream.**

> **Werdykt:** kod pochodzi z `ikidd/pocket-tts-wyoming` w commicie `bec7e57` (14.11.2026). Nasza adaptacja jest istotna, ale nie jest clean-roomem: w serwerze pozostały rozpoznawalne fragmenty upstreamu. Repozytorium upstream **nie ma licencji**, a plik `LICENSE` z MIT został w nim usunięty w pierwszym commicie adaptacji. Link w Credits jest potrzebny, ale nie zastępuje zgody licencyjnej. Wydzielenie TTS do osobnego repozytorium nie rozwiązuje problemu, bo zmienia miejsce dystrybucji, a nie prawa do kodu.

To nie jest opinia prawna. Poniżej są ustalenia techniczno-licencyjne i rekomendowana kolejność naprawy; przed publikacją lub komercjalizacją warto skonsultować je z prawnikiem.

## 1. Z której wersji pochodzi kod

### 1.1 Commit bazowy

| Element | Wartość |
|---|---|
| Repozytorium | [`ikidd/pocket-tts-wyoming`](https://github.com/ikidd/pocket-tts-wyoming) |
| Commit bazowy | [`bec7e57684b5f67cf829e56bab2fbd23b6e2eb46`](https://github.com/ikidd/pocket-tts-wyoming/commit/bec7e57684b5f67cf829e56bab2fbd23b6e2eb46) |
| Data commita | 2026-11-14T05:37:43Z |
| Ostatnia zmiana `wyoming_tts_server.py` przed bazą | [`44263d8`](https://github.com/ikidd/pocket-tts-wyoming/commit/44263d86a81dbbd221a311a0088ac7b171d1cb49), 27.08.2026 |
| Ostatnia zmiana `Dockerfile` przed bazą | [`1e7c320`](https://github.com/ikidd/pocket-tts-wyoming/commit/1e7c32018cba7f5ec4656d77a3b4f2476747ec1f), 28.08.2026 |
| Upstream HEAD w dniu audytu | [`a7fd9c9`](https://github.com/ikidd/pocket-tts-wyoming/commit/a7fd9c9bd4928f501d2e5408945493a027316382), 24.11.2026 |

Kod serwera jest identyczny od `44263d8` do `a7fd9c9`; upstream ma od bazy 10 nowszych commitów, ale wszystkie dotyczą wyłącznie `README.md` (daty i SHA). To potwierdza `git diff --stat bec7e57 origin/master`:

```text
 README.md | 6 +++---
 1 file changed, 3 insertions(+), 3 deletions(-)
```

Repozytorium upstream **nie ma tagów ani release’ów** (`git tag -l` puste, GitHub Releases puste). Nie da się więc powiedzieć „wersja 1.0.1”; jedynym stabilnym identyfikatorem jest commit `bec7e57`. String `version="1.0.1"` w `TtsProgram` został odziedziczony z pierwszego commita i nie jest wersją tego adaptera.

### 1.2 Skąd wiemy, że to dokładnie ten commit

Klon `../tts-pocket-wyoming/` powstał jako płytki klon upstreamu (w trakcie audytu pobrano pełną historię), a `.git/logs/HEAD` zapisuje moment klonowania:

```text
0000000000000000000000000000000000000000 bec7e57... Paweł ... clone: from https://github.com/ikidd/pocket-tts-wyoming.git
```

Data klonowania to 2026-11-14T15:36:51Z. `git merge-base HEAD origin/master` wskazuje `bec7e57`; zatem cała lokalna praca w tym katalogu zaczęła się od tego commita. Lokalne zmiany nie zostały jednak zacommitowane:

```text
 M Dockerfile
 M wyoming_tts_server.py
?? POLISH.md
?? rust_tts_backend.py
?? tts_render_worker.py
```

### 1.3 Dwie kopie, jedna produkcja

| Ścieżka | Rola | Stan |
|---|---|---|
| `../tts-pocket-wyoming/` (klon) | klon roboczy upstreamu; wariant z backendem Python/Torch i opcjonalnym Rust | zmiany niezacommitowane; `Dockerfile` nie odtwarza produkcyjnego wariantu Rust |
| [`tts-pocket-wyoming/`](../tts-pocket-wyoming/) (repo) | kopia wdrożeniowa w publicznym repozytorium Domuno | zacommitowana; używana przez [`Dockerfile`](../tts-pocket-wyoming/Dockerfile) i [`run.sh`](../run.sh#L106-L111) |

Kopia w repozytorium jest wersją produkcyjną: [`Dockerfile`](../tts-pocket-wyoming/Dockerfile#L1-L17) kopiuje binarkę Rust i `assets/LICENSE`, a [`run.sh`](../run.sh#L106-L111) sprawdza obecność modelu, tokenizera, głosu i binarki. Klon `../tts-pocket-wyoming/` jest wersją roboczą; jego `Dockerfile` buduje ścieżkę Python/Torch i nie dostarcza binarki ani configu Rust, więc nie odtwarza produkcji. Opisuje to też `POLISH.md`.

Obie kopie są rozjechane:

| Plik | `../tts-pocket-wyoming/` (klon) | `tts-pocket-wyoming/` (repo) | Różnica między kopiami |
|---|---:|---:|---:|
| `wyoming_tts_server.py` | 695 linii | 466 linii | +166 / −395 |
| `rust_tts_backend.py` | 343 linie | 335 linii | +41 / −49 |
| `tts_render_worker.py` | 171 linii | 215 linii | +45 / −1 |

### 1.4 Poziom pewności

| Twierdzenie | Pewność | Podstawa |
|---|---|---|
| kopia robocza pochodzi z `bec7e57` | potwierdzone | reflog klonu, `merge-base`, drzewo Git |
| kopia w repozytorium wywodzi się z tej samej adaptacji | wysokie, ale bez pinu | zbieżne fragmenty kodu, data commita, brak zapisanego SHA upstreamu |
| produkcją jest kopia w repozytorium | potwierdzone | Dockerfile, `run.sh`, dokumentacja operacyjna |
| upstream nie ma licencji | potwierdzone | drzewo Git, GitHub API, brak `LICENSE` |
| kod upstreamu nie zmienił się od bazy | potwierdzone | `git diff` do `origin/master` |
| kopia w repozytorium nie zapisuje commita bazowego | potwierdzone | brak pliku provenance w historii TTS |

Ostatni wiersz jest luką audytową: w kopii w repozytorium nie ma maszynowo czytelnego zapisu, z którego commita upstreamu powstał serwer. To trzeba dodać niezależnie od tego, jak zakończy się sprawa licencji.

### 1.5 Pułapka: `Last Upstream SHA` w README

W `README.md` upstreamu, również w wersji z bazy, występują dwie linie `Last Upstream SHA`:

```text
- **Last Upstream SHA**: 6f9dd250c24ee85cecc5587902a684f0d82b2a0d
- **Last Upstream SHA**: 9e8413ebc128dcbef682cce1906c7135f5b1e281
```

Obie dotyczą **`kyutai-labs/pocket-tts`**, a nie `ikidd/pocket-tts-wyoming`:

- `6f9dd25` to commit kyutai z 19.01.2026,
- `9e8413e` to commit kyutai z 13.11.2026.

Workflow upstreamu codziennie sprawdza HEAD kyutai i przebudowuje obraz, gdy ten się zmieni. To nie jest pin adaptera. Dla naszej adaptacji właściwym pinem jest `bec7e57`; `9e8413e` opisuje jedynie stan silnika Python, przeciw któremu budowano obraz w chwili klonowania. Produkcja Domuno używa natomiast Rusta `v4.0.1`.

## 2. Co zmieniliśmy

### 2.1 Skala zmian

| Plik | Zmiana względem `bec7e57` | Uwaga |
|---|---:|---|
| `../tts-pocket-wyoming/wyoming_tts_server.py` (klon) | +429 / −247 | wariant roboczy, 695 linii |
| `../tts-pocket-wyoming/Dockerfile` (klon) | +3 / −4 | Python/Torch, nieprodukcyjny |
| `../tts-pocket-wyoming/tts_render_worker.py` (klon) | nowy, 171 linii | kolejka i most PCM |
| `../tts-pocket-wyoming/rust_tts_backend.py` (klon) | nowy, 343 linie | adapter lokalnego daemona Rust |
| `tts-pocket-wyoming/wyoming_tts_server.py` (repo) | +334 / −381 | wersja produkcyjna, 466 linii |
| `tts-pocket-wyoming/tts_render_worker.py` (repo) | nowy, 215 linii | zawiera `TextStream` |
| `tts-pocket-wyoming/rust_tts_backend.py` (repo) | nowy, 335 linii | Rust-only, bez `pocket_tts` |
| `tts-pocket-wyoming/prepare_assets.py` (repo) | nowy, 64 linie | pobieranie i weryfikacja assetów |
| `tts-pocket-wyoming/Dockerfile` (repo) | nowy, 17 linii | obraz Rust na ARM64 |

To liczby linii, nie miara własności. Prawnie istotne jest to, które fragmenty są kontynuacją wyrażenia z upstreamu, a które są naszą samodzielną pracą.

### 2.2 Co zostało z upstreamu

W obu kopiach zachowana została struktura i duże fragmenty oryginału:

- klasa `PocketTTSEventHandler` i dyspozytor zdarzeń Wyoming (`Describe`, `Synthesize`, `SynthesizeStart/Chunk/Stop`) — [`tts-pocket-wyoming/.../wyoming_tts_server.py`](../tts-pocket-wyoming/wyoming_tts_server.py#L51);
- normalizacja nazwy głosu (`pocket-tts-*`), wybór głosu domyślnego i obsługa błędów;
- algorytm przycinania „sacrificial prefix” (`PREFIX_MIN_DURATION`, `PREFIX_MAX_DURATION`, `PREFIX_SILENCE_GAP`) — w kopii w repo niemal dosłownie w [`_finish_pcm_arrays`](../tts-pocket-wyoming/wyoming_tts_server.py#L359-L423);
- debugowe zapisywanie WAV i część obsługi CLI;
- w wariancie roboczym dodatkowo Zeroconf i informacja Wyoming.

Zbieżność liniowa (przybliżenie, nie test prawny):

| Wersja | Linie wspólne z upstreamem | Udział w pliku lokalnym |
|---|---:|---:|
| `../tts-pocket-wyoming/` (klon) | 274 z 513 linii upstreamu | 39,4% linii lokalnych |
| `tts-pocket-wyoming/` (repo) | 143 z 513 linii upstreamu | 30,7% linii lokalnych |

To wystarcza, by mówić o **utworze zależnym**, a nie o niezależnym przepisaniu. Nawet gdyby udział był mniejszy, zachowanie charakterystycznej struktury i funkcji może mieć znaczenie; dlatego nie traktuję tych procentów jako rozstrzygnięcia prawnego.

### 2.3 Co dodaliśmy lub istotnie zmieniliśmy

Wspólne dla obu kopii:

1. **Polski model i głos.** Zamiast ośmiu głosów angielskich jest jeden głos `shefowl` i model `shefowl/pocket-tts-polish-6l`. Zmieniono `PREDEFINED_VOICE_NAMES`, domyślny głos, język `pl` i atrybucję w Wyoming Info.
2. **Praca poza pętlą zdarzeń.** Generacja trafia do `RenderWorker` z jednym zadaniem w locie; `asyncio` nie blokuje się na modelu. Nowy plik [`tts_render_worker.py`](../tts-pocket-wyoming/tts_render_worker.py#L143).
3. **Bufor i strumień PCM.** Ograniczony bufor początkowy (domyślnie 2 s), heurystyka RTF, limity 120 s PCM i 2048 znaków, przycinanie końcowych zer PCM, metryki bez treści tekstu i bezpieczne błędy.
4. **Adapter Rust.** Nowy [`rust_tts_backend.py`](../tts-pocket-wyoming/rust_tts_backend.py#L104) uruchamia lokalny daemon `pocket-tts`, waliduje WAV/PCM, dzieli tekst na zdania i domyka anulowaną generację. To nasz kod, nie kopia upstreamu.
5. **Build i wdrożenie.** Dockerfile, compose, `prepare_assets.py`, pobieranie przypiętej rewizji modelu i binarki ARM64 oraz weryfikacja SHA256.
6. **Dokumentacja.** `POLISH.md`, mapa kodu, raporty tail/streaming.

Różnice między kopiami:

| Obszar | `../tts-pocket-wyoming/` (klon) | `tts-pocket-wyoming/` (repo) |
|---|---|---|
| Silnik | Python/Torch lub Rust (`POCKET_TTS_BACKEND`) | tylko Rust `v4.0.1` |
| Zeroconf | tak | nie |
| Strumień tekstu | zbierany do `SynthesizeStop` | dzielenie na zdania i `TextStream` |
| Liczby | własna implementacja PL | `num2words` (LGPL) |
| Limit czasu | limity PCM | dodatkowy deadline 130 s |
| Docker | Python/Torch, nie odtwarza produkcji | Rust + `assets`, odtwarza produkcję |

## 3. Licencje

### 3.1 Upstream `ikidd/pocket-tts-wyoming`: brak licencji

To najważniejsze ustalenie audytu.

| Sprawdzenie | Wynik |
|---|---|
| `LICENSE` w drzewie `bec7e57` | brak |
| GitHub API `license` | `null` |
| `GET /repos/ikidd/pocket-tts-wyoming/license` | HTTP 404 |
| Tagi / release’y | brak |
| Wzmianka o licencji w README | brak |

Historia Git pokazuje, dlaczego łatwo o pomyłkę:

- pierwszy commit adaptera [`cea5915`](https://github.com/ikidd/pocket-tts-wyoming/commit/cea591550f45690daa4d3b79f86d52f5888a9173) („Initial commit: Pocket-TTS Wyoming protocol server”) **usunął `LICENSE`**;
- rodzicem tego commita jest [`c4960e6`](https://github.com/kyutai-labs/pocket-tts/commit/c4960e6fd92afc57d556a599025f9965469ec26f) z `kyutai-labs/pocket-tts`, a usunięty plik był tekstem MIT kyutai;
- nowy kod serwera został dodany w tym samym commicie, już bez tekstu licencji.

To nie znaczy, że serwer jest objęty MIT. Licencja MIT z rodzica obejmowała kod kyutai obecny w rodzicu; nowy kod adaptera nie jest jej automatycznym następcą. Usunięcie pliku `LICENSE` nie cofa praw do starego kodu kyutai, ale nie nadaje licencji nowemu kodowi.

Autorzy pliku serwera w historii upstreamu to co najmniej:

- `ikidd <ikidd@protonmail.com>` — 3 commity,
- `Rich Ahrens <rich@ahrens.net>` — 3 commity,
- `Joshua M Smith <me@joshuamsmith.com>` — 1 commit.

To nie jest prawniczy podział praw do każdej linii, ale lista osób, z którymi trzeba wyjaśnić zgodę. Sam link do repozytorium i podanie autorów nie tworzy licencji.

GitHub dokumentuje domyślne prawo autorskie wprost: bez licencji „no one may reproduce, distribute, or create derivative works from your work”, przy czym publiczne repozytorium daje innym użytkownikom GitHub prawo do przeglądania i forkowania w ramach GitHub. To nie jest licencja na redystrybucję zmodyfikowanej wersji poza GitHubem. Zobacz [GitHub Docs — Licensing a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository).

**Konsekwencja:** obecny publiczny `p4vv37/domuno` publikuje [`tts-pocket-wyoming/wyoming_tts_server.py`](../tts-pocket-wyoming/wyoming_tts_server.py) pod zbiorczą licencją MIT. To jest publikacja kodu zależnego od upstreamu, dla którego nie mamy udokumentowanej zgody. Nie chodzi tylko o przyszłe „osobne repo TTS” — plik jest już w `main` publicznego repozytorium.

### 3.2 Co jeszcze trzeba zachować

| Komponent | Wersja / referencja | Licencja | Co zachować | Stan w projekcie |
|---|---|---|---|---|
| Serwer `ikidd/pocket-tts-wyoming` | `bec7e57` | **brak licencji** (domyślne prawo autorskie) | autorów, URL, commit, datę, zakres zmian; **pisemną zgodę lub licencję** | brak zgody; plik objęty MIT w repo |
| `kyutai-labs/pocket-tts` | `v4.0.1` (Rust, produkcja) | MIT | pełny tekst MIT + wskazanie Kyutai | `assets/LICENSE` trafia do obrazu, ale brak notices w repo |
| Binarka Rust `pocket-tts` | `v4.0.1`, `3.3.0` w Cargo | MIT | tekst licencji z archiwum | [`prepare_assets.py`](../tts-pocket-wyoming/prepare_assets.py#L54) wyciąga `LICENSE`; `Dockerfile` kopiuje go do `/app/LICENSE-pocket-tts` |
| Model `shefowl/pocket-tts-polish-6l` | rewizja `cd9047e` | CC BY 4.0 | autora, link do licencji, informację o zmianach, tekst/URI licencji | brak notices; `config.yaml` jest modyfikowany lokalnie |
| Głos referencyjny `reference.wav` | z modelu | CC BY 4.0 (CML-TTS) | atrybucję `ylacombe/cml-tts` i `shefowl`, link, informację o braku zmian | brak notices; dodatkowo polityka zakazu impersonacji z karty modelu |
| `wyoming` | `1.8.0` | MIT | tekst licencji | brak notices |
| `numpy` | `2.2.6` | BSD-3-Clause | copyright, warunki, disclaimer | brak notices |
| `num2words` | `0.5.14` | LGPL-2.1 | tekst licencji, źródło/offer, możliwość podmiany | brak notices; zależność LGPL w obrazie |

Szczegóły modelu: karta `shefowl/pocket-tts-polish-6l` deklaruje `license: cc-by-4.0`, a `reference.wav` jest klipem z CML-TTS (CC BY 4.0), redystrybuowanym przez shefowl. Karta zawiera też prośbę o nieużywanie modelu do impersonacji bez zgody; to nie jest warunek samej licencji CC BY, ale istotna polityka użycia. Zobacz [kartę modelu](https://huggingface.co/shefowl/pocket-tts-polish-6l) i [rewizję `cd9047e`](https://huggingface.co/shefowl/pocket-tts-polish-6l/tree/cd9047ee2fc9d1eb40ed38673d8b220485713863).

Wymagania CC BY 4.0: wskazać twórcę, zachować oznaczenia, podać link do licencji, wskazać zmiany i nie nakładać dodatkowych ograniczeń. Pełny tekst: [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/legalcode.en).

Wymagania MIT: dołączyć tekst copyright i permission notice do kopii lub istotnych części. Tekst Kyutai nie zawiera osobnej linii `Copyright (c)`, dlatego w notices należy wskazać projekt Kyutai jako źródło.

Zastrzeżenie do binarki Rust: linkuje ona statycznie wiele crate’ów cargo (m.in. `axum`, `tokio`, `serde`, `sentencepiece`) na różnych licencjach permissive, a release `v4.0.1` dokłada tylko jeden tekst MIT. To ograniczenie upstreamu, ale pełny compliance obrazu wymaga notices również dla tych zależności. Ten audyt ich nie enumeruje. Podobnie warstwa bazowa obrazu (Debian, Python, `uv`) ma własne notices poza zakresem tego dokumentu.

### 3.3 Co obecnie mówi projekt, a co jest prawdą

| Miejsce | Obecna deklaracja | Problem |
|---|---|---|
| [`README.md`](../README.md#L19) | badge „License: MIT” | sugeruje MIT dla całego repo, w tym adaptowanego serwera |
| [`README.md`](../README.md#L436-L438) | „MIT. See LICENSE” | brak wyłączenia pliku zależnego od upstreamu |
| [`LICENSE`](../LICENSE) | MIT, copyright 2026 Paweł | nie określa zakresu; nie może relicencjonować kodu ikidd |
| [`README.md`](../README.md#L428) | link do `ikidd/pocket-tts-wyoming` | brak commita, autorów, statusu licencji i opisu zmian |
| `TtsProgram` | `version="1.0.1"`, atrybucja tylko `shefowl` | string wersji odziedziczony; brak atrybucji adaptera i Kyutai |
| `../tts-pocket-wyoming/` (klon) | brak `LICENSE`; README upstreamu reklamuje obraz `ghcr.io/ikidd/...` | kopia robocza nie ma własnej informacji licencyjnej; łatwo pomylić ją z upstreamem |
| `__pycache__/` | pliki `.pyc` w katalogu roboczym | nie powinny trafić do publikacji |

## 4. Co zrobić przed dalszą publikacją

### 4.1 Blokery P0

1. **Uzyskać pisemną zgodę lub licencję od autorów upstreamu.** Potrzebna jest zgoda na modyfikację i redystrybucję kodu z commita `bec7e57` (najlepiej: MIT dla repozytorium i wcześniejszych commitów). Zapisać ją w repo, np. `LICENSES/ikidd-pocket-tts-wyoming.txt`, wraz z URL, datą, commitami i tekstem licencji. Sama odpowiedź „tak” w issue jest lepsza niż nic, ale dokument w repo jest trwalszy.
2. **Jeżeli zgody nie ma: nie publikować dalej i rozważyć zdjęcie pliku z publicznego repo.** Alternatywy: (a) zatrzymać repo jako prywatne, (b) napisać serwer od nowa jako clean-room na podstawie protokołu Wyoming, bez kopiowania struktury i fragmentów ikidd, (c) używać niezmodyfikowanego upstreamu jako zewnętrznej zależności, jeśli jego własna dystrybucja jest zgodna z prawem.
3. **Poprawić zakres MIT.** MIT w `LICENSE` powinien jasno obejmować tylko kod, do którego mamy prawa. Adaptowany serwer trzeba wyłączyć z tej deklaracji do czasu uzyskania zgody. Nie wystarczy zmienić badge; trzeba zmienić tekst w README i `LICENSE`/`NOTICE`.
4. **Dodać `THIRD_PARTY_NOTICES.md`** z pełnym tekstem MIT Kyutai, atrybucją modelu i CML-TTS, informacją o modyfikacjach oraz wykazem zależności. Umieścić go także w obrazie (np. `COPY THIRD_PARTY_NOTICES.md /app/`).
5. **Dodać provenance.** W repo powinien być krótki plik, np. `docs/tts-provenance.md`, z: URL upstreamu, commitem `bec7e57`, datą, autorami, zakresem naszych zmian i linkiem do tego audytu. To zastępuje niejednoznaczne `Last Upstream SHA`.

Szkic prośby do autorów (do wysłania w issue lub e-mailem):

> Hi — I maintain a local Home Assistant voice project. Our Wyoming TTS server is adapted from `ikidd/pocket-tts-wyoming` at commit `bec7e57` (2026-11-14). The repository currently has no `LICENSE`, and we want to redistribute our modified version with proper attribution. Could you grant a license (for example MIT) covering that commit and earlier versions, or confirm in writing that the code may be modified and redistributed? We will keep the copyright notice, the commit reference and a description of our changes. Thank you.

### 4.2 P1 — porządki i zgodność techniczna

6. **Naprawić Wyoming Info.** `TtsProgram.version` nie powinien udawać wersji upstreamu; ustawić własną wersję adaptacji (np. `1.1.0-pl`) i opisać ją w provenance. W atrybucji wskazać model `shefowl` oraz — w miarę możliwości — źródło adaptera.
7. **Uporządkować dwie kopie.** `tts-pocket-wyoming/` (repo) jest produkcją; `../tts-pocket-wyoming/` (klon) to wersja robocza. Przed publikacją albo przenieść potrzebne zmiany do jednej kopii, albo zarchiwizować klon i usunąć go z drzewa. Dwie rozjechane wersje utrudniają audyt i zwiększają ryzyko, że opublikowana zostanie niewłaściwa.
8. **Zdecydować o `num2words`.** To LGPL-2.1 w obrazie. Albo dodać tekst licencji i źródło/offer, albo zastąpić je własną implementacją liczb, która istnieje w wariancie roboczym `tts-pocket-wyoming/rust_tts_backend.py` (poza tym repozytorium). Wariant roboczy nie ma tej zależności.
9. **Dodać test/check licencyjny.** Np. CI sprawdzający, że `THIRD_PARTY_NOTICES.md` istnieje, zawiera `bec7e57`, a `README` nie deklaruje MIT dla `wyoming_tts_server.py` bez wpisu o zgodzie.

### 4.3 Minimalny zestaw informacji do zachowania

- [ ] URL upstreamu, commit `bec7e57`, data i autorzy.
- [ ] Status licencji upstreamu i — po uzyskaniu — pełny tekst zgody.
- [ ] Opis zmian: polski głos/model, worker, PCM, Rust, sentence streaming, build.
- [ ] MIT Kyutai i binarki Rust.
- [ ] CC BY 4.0 dla modelu i głosu referencyjnego (CML-TTS).
- [ ] Notices dla `wyoming`, `numpy`, `num2words`.
- [ ] Zakres własnego MIT (tylko kod oryginalny).
- [ ] Informacja „independent project, not affiliated”.

## 5. Szablon informacji licencyjnych

Poniższy szkic jest do uzupełnienia; **nie wklejać go bez rozstrzygnięcia punktu 4.1**.

```markdown
# Third-party notices

## Adapted Wyoming server

This repository contains a modified version of the Wyoming TTS server from
ikidd/pocket-tts-wyoming.

- Source: https://github.com/ikidd/pocket-tts-wyoming
- Base commit: bec7e57684b5f67cf829e56bab2fbd23b6e2eb46 (2026-11-14)
- Upstream authors: ikidd, Joshua M Smith, Rich Ahrens
- Upstream license: NO LICENSE DECLARED at the base commit. Copyright remains
  with the upstream authors. [If permission is granted, replace this paragraph
  with the exact grant and license text, covering the base commit.]
- Changes: Polish voice and model, render worker, PCM buffering/streaming,
  Rust adapter, sentence-level streaming, build and asset changes.

## Pocket TTS

Pocket TTS, https://github.com/kyutai-labs/pocket-tts, v4.0.1, MIT.
Copyright Kyutai. [Full MIT text from the release LICENSE file.]

Includes the Rust runtime `pocket-tts-3.3.0-aarch64-unknown-linux-gnu`.

## Polish model and reference voice

shefowl/pocket-tts-polish-6l, revision cd9047ee2fc9d1eb40ed38673d8b220485713863,
CC BY 4.0, https://creativecommons.org/licenses/by/4.0/.
Changes: model config asset URLs were localized for offline use; weights are
unmodified. Reference clip: CML-TTS (ylacombe/cml-tts), CC BY 4.0, redistributed
by shefowl; no changes to reference.wav.

## Python dependencies

- wyoming 1.8.0 — MIT
- NumPy 2.2.6 — BSD-3-Clause
- num2words 0.5.14 — LGPL-2.1 (source: https://github.com/savoirfairelinux/num2words)

## Our code

Original code in this repository is Copyright (c) 2026 Paweł and is licensed
under MIT (see LICENSE). This MIT grant does not cover the adapted server in
the first section.
```

Proponowany fragment README zamiast zbiorczego „MIT”:

```markdown
MIT applies to the original code in this repository. The adapted Wyoming
server in `tts-pocket-wyoming/wyoming_tts_server.py` is based on
ikidd/pocket-tts-wyoming at commit bec7e57, which declares no license. It is
not covered by this MIT grant and must not be redistributed without permission
from its authors. See THIRD_PARTY_NOTICES.md.
```

Po uzyskaniu zgody drugie zdanie trzeba zastąpić wskazaniem uzyskanej licencji i zachować jej tekst.

## 6. Dowody i ograniczenia

### 6.1 Użyte dowody

- Historia i drzewo Git w klonie `../tts-pocket-wyoming/`:
  - `.git/logs/HEAD` — klon z `bec7e57`,
  - `git merge-base HEAD origin/master` → `bec7e57`,
  - `git diff --stat bec7e57 origin/master` → tylko `README.md`,
  - `git shortlog -sne --all -- wyoming_tts_server.py` → autorzy,
  - `git show cea5915 --stat` → usunięcie `LICENSE`,
  - `git show cea5915^:LICENSE` → tekst MIT kyutai.
- GitHub API:
  - `https://api.github.com/repos/ikidd/pocket-tts-wyoming` → `license: null`,
  - `https://api.github.com/repos/ikidd/pocket-tts-wyoming/license` → 404,
  - `https://api.github.com/repos/kyutai-labs/pocket-tts` → MIT,
  - `https://api.github.com/repos/p4vv37/domuno` → publiczne repo, wykryte MIT,
  - `https://api.github.com/repos/p4vv37/domuno/contents/tts-pocket-wyoming/wyoming_tts_server.py` → plik w `main`, blob `42c5877`, 21099 B (zgodny z lokalnym).
- Hugging Face:
  - `https://huggingface.co/api/models/shefowl/pocket-tts-polish-6l` → tag `license:cc-by-4.0`,
  - karta modelu → CC BY 4.0, CML-TTS dla `reference.wav`.
- Kyutai:
  - `https://github.com/kyutai-labs/pocket-tts/blob/v4.0.1/LICENSE` → MIT,
  - `rust/Cargo.toml` → `license = "MIT"`, wersja pakietu `3.3.0`.
- Licencje zależności: PyPI JSON dla `wyoming==1.8.0` (MIT), `numpy==2.2.6` (BSD-3-Clause), `num2words==0.5.14` (LGPL; `COPYING` = LGPL 2.1).

### 6.2 Ograniczenia

- To nie jest opinia prawna ani rozstrzygnięcie o naruszeniu; to identyfikacja stanu i ryzyk.
- Nie sprawdzano, czy autorzy upstreamu udzielili zgody poza GitHubem (e-mail, prywatna rozmowa). Audyt pokazuje, że w publicznych źródłach jej nie ma.
- Pochodzenie kopii w repo jest wywnioskowane z treści i czasu; nie ma zapisanego pinu upstreamu. To luka, którą trzeba zamknąć.
- Nie weryfikowano runtime, jakości audio ani testów; audyt dotyczy provenance i licencji.
- Stan źródeł: 24.11.2026. Upstream może w międzyczasie dodać `LICENSE`; wtedy trzeba sprawdzić, czy obejmuje ono commit `bec7e57`, a nie tylko przyszłe zmiany.

## 7. Krótkie odpowiedzi

**Z której wersji pochodzi kod?**  
Z commita `bec7e57684b5f67cf829e56bab2fbd23b6e2eb46` z 14.11.2026. Treść serwera jest identyczna od `44263d8` (27.08.2026), a `Dockerfile` od `1e7c320` (28.08.2026). To nie jest tag; repo nie ma tagów.

**Co zmieniliśmy?**  
Polski głos i model, pracę poza pętlą zdarzeń, bufor/strumień PCM i limity, adapter Rust, strumień zdań, build/assety i dokumentację. Zachowaliśmy jednak strukturę i fragmenty upstreamu, w tym algorytm przycinania prefiksu.

**Jakie informacje licencyjne trzeba zachować?**  
Autorów i commit upstreamu, status „brak licencji” i — po uzyskaniu — tekst zgody; MIT Kyutai i binarki Rust; CC BY 4.0 modelu i CML-TTS; notices `wyoming`, `numpy`, `num2words`; zakres własnego MIT.

**Czy link w Credits wystarcza?**  
Nie. Link identyfikuje projekt, ale nie zastępuje licencji, nie wskazuje commita ani zakresu zmian i nie daje prawa do redystrybucji.

**Czy osobne repo to rozwiąże?**  
Nie. To nadal dystrybucja tego samego utworu zależnego; zmienia się adres, nie prawa. Najpierw zgoda lub clean-room, potem wydzielenie.
