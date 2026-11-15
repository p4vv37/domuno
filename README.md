# Lokalny asystent Home Assistant na Arduino VENTUNO Q

Whisper Turbo i Qwen2.5-3B działają na NPU, polski Pocket TTS na CPU.
LVA obsługuje mikrofon, „Okay Nabu” i głośnik. Home Assistant zarządza rozmową
oraz wykonuje działania. Może działać na osobnym komputerze.

Konfigurujesz **jeden plik `.env`**, a zestaw obsługujesz przez **`./run.sh`**.
Instrukcja dotyczy czystej instalacji. Nie uruchamiaj jej obok istniejącego
wdrożenia korzystającego z tych samych portów i nazw kontenerów.

## 1. Przygotuj Arduino

Potrzebne są: Ubuntu ARM64 ze sterownikami Qualcomm dla VENTUNO Q, Docker
z Compose, Python 3.10+, `uv`, sesja `systemd --user` i kilka GB wolnego miejsca.
Pobieranie modeli i budowanie obrazów wymaga internetu.

```bash
sudo apt update
sudo apt install -y curl git python3 libatomic1 libglib2.0-0
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/install-uv.sh
sh /tmp/install-uv.sh
export PATH="$HOME/.local/bin:$PATH"
git clone https://github.com/p4vv37/ha-local-agent.git
cd ha-local-agent
```

Zainstaluj GenieX, zgodnie z [instrukcją producenta](https://geniex.aihub.qualcomm.com/en/run/cli/install).
Na poprawnie przygotowanym obrazie Arduino:

```bash
curl -fsSL https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-geniex/install.sh -o /tmp/install-geniex.sh
sh /tmp/install-geniex.sh --version v0.9.3
geniex --version
docker ps
systemctl --user show-environment
```

Sprawdź wersję 0.9.3. Typowa ścieżka to `~/.local/bin/geniex`.
Przy innej ustaw `GENIEX_BIN` w `.env`. Sterowniki muszą pochodzić z obrazu/BSP
właściwego dla płytki. Sam instalator GenieX nie zastępuje sterowników NPU.

## 2. Pobierz zasoby i ustaw ścieżki

```bash
cp .env.example .env
chmod 600 .env
./run.sh prepare
```

`prepare` pobiera Qwena Q4_0, tokenizer, artefakt Whisper Turbo oraz polski model,
głos i binarkę Pocket TTS Rust. Nie uruchamia modeli. Przygotowanie jest oddzielone
od startu, żeby pobierania nie powtarzać podczas zwykłego uruchamiania.

W `.env` ustaw:

| Pole | Wartość |
|---|---|
| `GGUF` | Pełna ścieżka do pliku Qwen Q4_0, wskazana przez `geniex pull` |
| `QNN_LIB_DIR` | Katalog zgodnych bibliotek HTP, zawierający `libQnnHtp.so` |
| `LVA_USER_ID`, `LVA_USER_GROUP` | Wyniki `id -u` oraz `id -g` |
| `AUDIO_INPUT_DEVICE`, `AUDIO_OUTPUT_DEVICE` | Urządzenia mikrofonu i głośnika |

STT wymaga **QAIRT 2.50.0.260828221209**, **ONNX Runtime 1.30.0** i zgodnego
pluginu QNN EP. Dostarcz pakiety `.whl` dla Linux ARM64 i CPython 3.12 do
`stt/runtime/`, według [instrukcji ONNX Runtime/QNN](https://onnxruntime.ai/docs/execution-providers/QNN-ExecutionProvider.html).
Bibliotek dostawcy nie dołączamy do repo. Katalog HTP bywa dostępny pod
`~/.local/share/geniex/qairt/htp-files`, ale trzeba potwierdzić jego wersję.
To jedyny etap zależny od konkretnego obrazu systemu i dostępu do pakietów dostawcy.

Dźwięk korzysta z PulseAudio lub PipeWire-Pulse. Domyślny socket to
`/run/user/<LVA_USER_ID>/pulse/native`. Aby wyświetlić urządzenia audio, ustaw
chwilowo `LIST_DEVICES=1`, uruchom LVA i sprawdź jego log, potem przywróć pustą wartość.

## 3. Sprawdź i uruchom

```bash
./run.sh check
./run.sh start
./run.sh status
```

`check` niczego nie instaluje ani nie uruchamia. Wypisuje `OK` lub `BRAK`
dla wymaganych narzędzi, plików, środowiska NPU i audio.
`start` nie przechodzi dalej, dopóki wszystkie te pozycje nie są dostępne.

`start` buduje kontenery i instaluje usługi. Uruchamia kolejno:

1. Qwena, z jedną próbą załadowania i krótkim testem odpowiedzi.
2. Whispera, po zakończonym starcie Qwena.
3. Licznik kontekstu i bramkę z routerem, korzystające z venv utworzonego przez `uv`.
4. Pocket TTS i LVA.

Usługi są instalowane z rzeczywistymi ścieżkami repo. Nie kopiujesz ani nie
edytujesz plików systemd. `setup.sh` to instalator wywoływany przez `run.sh`.
Nie restartuje już działającego backendu Qwena. Zmianę modelu lub jego kontekstu
przeprowadzaj jako osobny zimny start płytki, nie przez kolejne próby ładowania.

W HA dodaj:

| Integracja | Adres |
|---|---|
| Wyoming STT | Host: IP Arduino, port `10301` |
| Wyoming TTS | Host: IP Arduino, port `10201`, głos `shefowl`, język `pl` |
| [Local OpenAI LLM](https://github.com/skye-harris/hass_local_openai_llm), przez HACS | `http://<IP-Arduino>:18191/v1` |
| ESPHome, dla LVA | Host: IP Arduino, port `6053`, bez `http://` |

W Local OpenAI wybierz model `bartowski/Qwen2.5-3B-Instruct-GGUF:Q4_0`,
typ `llama.cpp` i narzędzia **Assist / Device control**. Udostępnij wybrane encje.
W ustawieniach asystenta przypisz polskie STT, agenta i TTS, a następnie ten
asystent do satelity LVA. Najpierw sprawdź neutralne pytanie, potem wybrane działanie.

## Codzienna obsługa

```bash
./run.sh status
./run.sh stop
./run.sh start
journalctl --user -u qwen-ha-gateway -n 30 --no-pager
docker logs --tail 30 linux-voice-assistant
```

Po ręcznym zatrzymaniu całego zestawu ponowne zimne ładowanie Qwena w tym samym
bootcie blokuje guard. W takim przypadku zrestartuj płytkę. Nie usuwaj jego blokady
w celu wymuszania kolejnych prób po FastRPC ENOMEM.

Autostart usług użytkownika bez logowania:

```bash
sudo loginctl enable-linger "$USER"
```

Jednostki Qwena, STT, bramki i licznika są włączane przez instalator. TTS i LVA
mają Docker `unless-stopped`. Zweryfikuj pełny restart na swoim Arduino.

## Co zostało w kodzie

```text
run.sh + .env.example   uruchamianie i wspólna konfiguracja
setup.sh               wewnętrzna instalacja usług przez uv
llm/backend.py         kontrola startu i gotowości modeli
stt/                   silnik QNN, walidacja modelu, protokół Wyoming
gateway/              proxy, router, katalog HA, licznik kontekstu
tts-pocket-wyoming/    adapter Rust, protokół i przesyłanie PCM
lva/                   konfiguracja gotowego satelity
tests/                 testy offline
```

Router obsługuje proste włączanie/wyłączanie i otwieranie/zamykanie.
Korzysta z bieżących nazw i schematów HA, dopuszcza drobne błędy STT i pyta
przy niejednoznaczności. Złożone komendy trafiają do Qwena. `DECIDE_APPLY=0`
w `.env` wyłącza router. Domyślny kontekst 10240 ma licznik z ostrożnym zapasem.
Przekroczenie budżetu daje 413, niedostępny licznik 503 przed inferencją.
Szacunek nie jest certyfikowanym odpowiednikiem natywnego tokenizera.

Bramka domyślnie działa bez klucza w zaufanym LAN. Opcjonalny
`QWEN_GATEWAY_API_KEY` ustaw w `.env` i w integracji HA. Nie wymaga klucza chmurowego.
Surowego audio nie zapisujemy. `.env`, modele, pakiety dostawcy i logi są ignorowane
przez Git. Kod korzysta z modeli i runtime'ów ich autorów, serwer TTS adaptowano
z [Pocket TTS Wyoming](https://github.com/ikidd/pocket-tts-wyoming), a satelita pochodzi
z [Linux Voice Assistant](https://github.com/OHF-Voice/linux-voice-assistant).

Testy bez pobierania czegokolwiek i bez uruchamiania usług:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Z `.venv/bin/python` zamiast `python3` testy obejmą też zainstalowane `simplemma`.
Testy protokołu używają atrap modeli. Nowe obrazy ARM64 i ich współpracę z NPU
trzeba sprawdzić na docelowej płytce.
